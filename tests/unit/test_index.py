"""Unit tests for the indexer — dedup, mtime skip, batch insert."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aurochs_recall.core.db import db_connect
from aurochs_recall.core.index import Indexer
from aurochs_recall.core.migrations.runner import run_migrations
from aurochs_recall.core.types import Drawer


def _setup_db(tmp_path: Path) -> Path:
    """Create a fresh DB with the baseline schema applied."""
    db_path = tmp_path / "recall.db"
    run_migrations(db_path)
    return db_path


def _make_drawer(content: str, *, source_id: str = "session-1:0") -> Drawer:
    return Drawer(
        source="claude_code",
        source_id=source_id,
        role="human",
        content=content,
        created_at=int(time.time()),
    )


# ---------------------------------------------------------------------------
# index_drawers — direct iterable path
# ---------------------------------------------------------------------------

class TestIndexDrawers:
    def test_inserts_new(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        idx = Indexer(db)
        drawers = [
            _make_drawer("hello world", source_id="s:0"),
            _make_drawer("goodbye world", source_id="s:1"),
            _make_drawer("third drawer", source_id="s:2"),
        ]
        added = idx.index_drawers(drawers)
        assert added == 3

        conn = db_connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM drawer_meta"
            ).fetchone()[0]
            assert count == 3
        finally:
            conn.close()

    def test_dedupes_by_content_hash(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        idx = Indexer(db)

        d1 = _make_drawer("same content", source_id="s:0")
        d2 = _make_drawer("same content", source_id="s:0")  # identical
        # First call inserts 1.
        assert idx.index_drawers([d1]) == 1
        # Second call inserts 0 — UNIQUE on (content_hash, source, source_id).
        assert idx.index_drawers([d2]) == 0

        conn = db_connect(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM drawer_meta"
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_different_source_id_is_different_drawer(self, tmp_path: Path) -> None:
        """Same content + role but different source_id → distinct drawers."""
        db = _setup_db(tmp_path)
        idx = Indexer(db)

        d1 = _make_drawer("hi", source_id="s:0")
        d2 = _make_drawer("hi", source_id="s:1")
        added = idx.index_drawers([d1, d2])
        assert added == 2
        assert d1.drawer_uid != d2.drawer_uid

    def test_fts_populated(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        idx = Indexer(db)
        idx.index_drawers([_make_drawer("the quick brown fox")])

        conn = db_connect(db)
        try:
            row = conn.execute(
                "SELECT content FROM drawers_fts WHERE drawers_fts MATCH 'quick'"
            ).fetchone()
            assert row is not None
            assert "quick brown fox" in row[0]
        finally:
            conn.close()

    def test_batch_boundary(self, tmp_path: Path) -> None:
        """Inserting exactly _BATCH_SIZE drawers must not crash on flush."""
        from aurochs_recall.core.index import _BATCH_SIZE  # type: ignore[attr-defined]

        db = _setup_db(tmp_path)
        idx = Indexer(db)
        drawers = [
            _make_drawer(f"content {i}", source_id=f"s:{i}")
            for i in range(_BATCH_SIZE + 5)
        ]
        added = idx.index_drawers(drawers)
        assert added == _BATCH_SIZE + 5


# ---------------------------------------------------------------------------
# index_source — single-process walk via Ingestor
# ---------------------------------------------------------------------------

class _StubIngestor:
    """In-process test ingestor."""

    name = "stub"

    def __init__(self, files_to_drawers: dict[Path, list[Drawer]]) -> None:
        self._map = files_to_drawers

    def discover_files(self, root: Path) -> Iterator[Path]:
        for f in self._map:
            yield f

    def read_drawers(self, file_path: Path) -> Iterator[Drawer]:
        for d in self._map.get(file_path, []):
            yield d


class TestIndexSource:
    def test_indexes_all_files(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        idx = Indexer(db)

        f1 = tmp_path / "file1.md"
        f1.write_text("dummy", encoding="utf-8")
        f2 = tmp_path / "file2.md"
        f2.write_text("dummy", encoding="utf-8")

        ingestor = _StubIngestor({
            f1: [_make_drawer("from file1", source_id="f1:0")],
            f2: [
                _make_drawer("from file2 a", source_id="f2:0"),
                _make_drawer("from file2 b", source_id="f2:1"),
            ],
        })
        added = idx.index_source("stub", ingestor, tmp_path)
        assert added == 3

    def test_mtime_skip(self, tmp_path: Path) -> None:
        """Re-indexing without mtime change should skip files entirely."""
        db = _setup_db(tmp_path)
        idx = Indexer(db)

        f = tmp_path / "stable.md"
        f.write_text("content", encoding="utf-8")
        # Force mtime to a known value so the second run's mtime check is
        # deterministic regardless of filesystem timestamp resolution.
        os.utime(f, (1_700_000_000, 1_700_000_000))

        ingestor = _StubIngestor({f: [_make_drawer("hello", source_id="f:0")]})

        first = idx.index_source("stub", ingestor, tmp_path)
        assert first == 1

        # No mtime change → second run skips the file entirely.
        second = idx.index_source("stub", ingestor, tmp_path)
        assert second == 0

    def test_mtime_change_triggers_reindex(self, tmp_path: Path) -> None:
        db = _setup_db(tmp_path)
        idx = Indexer(db)

        f = tmp_path / "stale.md"
        f.write_text("content", encoding="utf-8")
        os.utime(f, (1_700_000_000, 1_700_000_000))

        ingestor1 = _StubIngestor({f: [_make_drawer("v1", source_id="f:0")]})
        idx.index_source("stub", ingestor1, tmp_path)

        # Bump mtime forward.
        os.utime(f, (1_700_000_100, 1_700_000_100))
        # And feed a new drawer this time.
        ingestor2 = _StubIngestor({f: [_make_drawer("v2", source_id="f:1")]})
        added = idx.index_source("stub", ingestor2, tmp_path)
        assert added == 1
