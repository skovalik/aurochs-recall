"""Unit tests for the ``run_index`` orchestrator.

Covers patches B2 (ingest_errors writes) and B3 (sources.toml
include/exclude pattern application).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.index import (
    _path_passes_filters,
    _record_ingest_error,
    _suggest_fix_hint,
    run_index,
)
from core.migrations.runner import run_migrations
from core.sources_config import SourceEntry


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_sources_toml(
    cfg_path: Path,
    db_path: Path,
    *,
    sources_blocks: list[str],
) -> None:
    """Render a sources.toml with the given source blocks (escape backslashes)."""
    db_str = str(db_path).replace("\\", "\\\\")
    body = "\n".join(sources_blocks)
    cfg_path.write_text(
        f"schema_version = 1\n\n"
        f"[database]\n"
        f'path = "{db_str}"\n\n'
        f"{body}\n",
        encoding="utf-8",
    )


def _make_jsonl_session(
    tmp_path: Path,
    *,
    name: str = "7d3e2a1b-6c4f-4d5e-9a8b-1f2c3d4e5f60",
    good_lines: int = 2,
    bad_lines: int = 1,
) -> Path:
    """Write a Claude Code-style jsonl session under ``tmp_path/<uuid>.jsonl``.

    Each good line is a valid jsonl record; bad lines are intentionally
    malformed so the ingestor's per-line warning fires.
    """
    sessions_dir = tmp_path / "claude_code_sessions"
    sessions_dir.mkdir()
    p = sessions_dir / f"{name}.jsonl"
    lines: list[str] = []
    for i in range(good_lines):
        lines.append(
            json.dumps(
                {
                    "role": "human" if i % 2 == 0 else "assistant",
                    "content": f"Lorem ipsum dolor sit amet record {i} consectetur adipiscing.",
                }
            )
        )
    for i in range(bad_lines):
        # Malformed JSONL: missing closing brace.
        lines.append('{"role":"human","content":"oh no')
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# B2 — _record_ingest_error helper
# ---------------------------------------------------------------------------


class TestRecordIngestError:
    def test_writes_row_with_all_fields(self, tmp_path: Path) -> None:
        db = tmp_path / "recall.db"
        run_migrations(db)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            _record_ingest_error(
                conn,
                source="claude_code",
                source_path="/tmp/foo.jsonl",
                reason="bad jsonl at line 4",
                fix_hint="check line 4",
            )
            conn.commit()
            rows = conn.execute(
                "SELECT source, source_path, reason, fix_hint, retry_count "
                "FROM ingest_errors"
            ).fetchall()
            assert len(rows) == 1
            r = rows[0]
            assert r["source"] == "claude_code"
            assert r["source_path"] == "/tmp/foo.jsonl"
            assert r["reason"] == "bad jsonl at line 4"
            assert r["fix_hint"] == "check line 4"
            assert r["retry_count"] == 0
        finally:
            conn.close()

    def test_no_fix_hint_writes_null(self, tmp_path: Path) -> None:
        db = tmp_path / "recall.db"
        run_migrations(db)
        conn = sqlite3.connect(str(db))
        try:
            _record_ingest_error(
                conn,
                source="claude_ai",
                source_path=None,
                reason="some reason",
                fix_hint=None,
            )
            conn.commit()
            row = conn.execute(
                "SELECT fix_hint, source_path FROM ingest_errors"
            ).fetchone()
            assert row[0] is None
            assert row[1] is None
        finally:
            conn.close()

    def test_swallows_missing_table(self, tmp_path: Path) -> None:
        """Pre-baseline DB without ingest_errors must not crash the indexer."""
        db = tmp_path / "empty.db"
        # No migrations — the table doesn't exist.
        conn = sqlite3.connect(str(db))
        try:
            # Should not raise.
            _record_ingest_error(
                conn,
                source="claude_code",
                source_path="/tmp/x",
                reason="anything",
                fix_hint=None,
            )
        finally:
            conn.close()


class TestSuggestFixHint:
    @pytest.mark.parametrize(
        "reason_keyword,expected_substring",
        [
            ("bad jsonl at line 4", "JSONL line"),
            ("non-object jsonl at line 8", "different schema version"),
            ("conversation has no message array", "chat_messages"),
            ("Conversation without uuid", "interrupted export"),
            ("Invalid JSON in /tmp/x", "truncated"),
            ("Cannot determine session UUID from path", "UUID"),
            ("Failed to read /tmp/x", "permissions"),
            ("Expected top-level JSON array", "top-level JSON array"),
        ],
    )
    def test_recognized_patterns(self, reason_keyword: str, expected_substring: str) -> None:
        hint = _suggest_fix_hint(reason_keyword)
        assert hint is not None
        assert expected_substring.lower() in hint.lower()

    def test_unknown_reason_returns_none(self) -> None:
        assert _suggest_fix_hint("a totally unrelated message") is None


# ---------------------------------------------------------------------------
# B2 — run_index records errors for malformed inputs
# ---------------------------------------------------------------------------


class TestRunIndexIngestErrors:
    def test_per_line_bad_jsonl_records_error(self, tmp_path: Path) -> None:
        """A claude_code session with one malformed line should produce one
        ingest_errors row tagged 'bad jsonl' AND still successfully index
        the good lines."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _make_jsonl_session(sessions_dir, good_lines=2, bad_lines=1)

        cfg = tmp_path / "sources.toml"
        db = tmp_path / "recall.db"
        sessions_root = sessions_dir / "claude_code_sessions"
        sessions_str = str(sessions_root).replace("\\", "\\\\")
        _write_sources_toml(
            cfg,
            db,
            sources_blocks=[
                f'[[sources]]\nname = "claude_code"\n'
                f'type = "claude_code"\npath = "{sessions_str}"\n'
                f'enabled = true',
            ],
        )

        rc = run_index(config_path=cfg, db_path=db)
        assert rc == 0

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            # Errors recorded.
            errs = conn.execute(
                "SELECT source, reason, fix_hint, source_path FROM ingest_errors"
            ).fetchall()
            assert len(errs) >= 1, "expected at least one ingest_errors row"
            # The bad-line warning should land here.
            assert any("bad jsonl" in (e["reason"] or "") for e in errs)
            # Source path should be set for per-line errors.
            assert all(e["source_path"] for e in errs)
            # Fix hints should be populated for known patterns.
            assert any(e["fix_hint"] for e in errs)

            # Good drawers still indexed.
            n_drawers = conn.execute(
                "SELECT COUNT(*) FROM drawer_meta"
            ).fetchone()[0]
            assert n_drawers >= 1
        finally:
            conn.close()

    def test_invalid_top_level_json_records_file_error(self, tmp_path: Path) -> None:
        """A claude_ai conversations.json with broken top-level JSON must
        record a file-level ingest_errors row."""
        export_dir = tmp_path / "claude_ai_export"
        export_dir.mkdir()
        bad = export_dir / "conversations.json"
        bad.write_text("{this is not valid json", encoding="utf-8")

        cfg = tmp_path / "sources.toml"
        db = tmp_path / "recall.db"
        bad_str = str(bad).replace("\\", "\\\\")
        _write_sources_toml(
            cfg,
            db,
            sources_blocks=[
                f'[[sources]]\nname = "claude_ai"\n'
                f'type = "claude_ai"\npath = "{bad_str}"\n'
                f'enabled = true',
            ],
        )

        rc = run_index(config_path=cfg, db_path=db)
        assert rc == 0  # broken file shouldn't crash the run

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            errs = conn.execute(
                "SELECT source, reason, fix_hint FROM ingest_errors"
            ).fetchall()
            assert len(errs) == 1
            assert errs[0]["source"] == "claude_ai"
            assert "invalid json" in errs[0]["reason"].lower() or \
                "ingesterror" in errs[0]["reason"].lower()
            assert errs[0]["fix_hint"] is not None
        finally:
            conn.close()

    def test_recall_errors_cli_shows_recorded_errors(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        """End-to-end: bad input → run_index records → recall errors prints."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _make_jsonl_session(sessions_dir, good_lines=1, bad_lines=2)

        cfg = tmp_path / "sources.toml"
        db = tmp_path / "recall.db"
        sessions_root = sessions_dir / "claude_code_sessions"
        sessions_str = str(sessions_root).replace("\\", "\\\\")
        _write_sources_toml(
            cfg,
            db,
            sources_blocks=[
                f'[[sources]]\nname = "claude_code"\n'
                f'type = "claude_code"\npath = "{sessions_str}"\n'
                f'enabled = true',
            ],
        )

        rc = run_index(config_path=cfg, db_path=db)
        assert rc == 0

        from cli.main import main as cli_main

        capsys.readouterr()  # clear
        rc = cli_main(["--db", str(db), "errors"])
        assert rc == 0
        out = capsys.readouterr().out
        # CLI output should NOT say "no ingest errors".
        assert "no ingest errors" not in out.lower()
        # Should reference the source and a hint.
        assert "claude_code" in out


# ---------------------------------------------------------------------------
# B3 — sources.toml exclude/include glob filtering
# ---------------------------------------------------------------------------


class TestPathPassesFilters:
    def test_no_filters_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "foo.md"
        p.write_text("x", encoding="utf-8")
        src = SourceEntry(name="x", type="markdown", path=str(tmp_path))
        assert _path_passes_filters(p, tmp_path, src) is True

    def test_none_source_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "foo.md"
        p.write_text("x", encoding="utf-8")
        assert _path_passes_filters(p, tmp_path, None) is True

    def test_exclude_drops_match(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "foo.md"
        nm.parent.mkdir()
        nm.write_text("x", encoding="utf-8")
        src = SourceEntry(
            name="x", type="markdown", path=str(tmp_path),
            exclude=("**/node_modules/**",),
        )
        assert _path_passes_filters(nm, tmp_path, src) is False

    def test_exclude_relative_pattern(self, tmp_path: Path) -> None:
        """``node_modules/**`` (relative form) should also work."""
        nm = tmp_path / "node_modules" / "foo.md"
        nm.parent.mkdir()
        nm.write_text("x", encoding="utf-8")
        src = SourceEntry(
            name="x", type="markdown", path=str(tmp_path),
            exclude=("node_modules/**",),
        )
        assert _path_passes_filters(nm, tmp_path, src) is False

    def test_include_only_allows_matching(self, tmp_path: Path) -> None:
        good = tmp_path / "notes" / "good.md"
        good.parent.mkdir()
        good.write_text("x", encoding="utf-8")
        bad = tmp_path / "drafts" / "bad.md"
        bad.parent.mkdir()
        bad.write_text("x", encoding="utf-8")

        src = SourceEntry(
            name="x", type="markdown", path=str(tmp_path),
            include=("notes/**",),
        )
        assert _path_passes_filters(good, tmp_path, src) is True
        assert _path_passes_filters(bad, tmp_path, src) is False

    def test_exclude_takes_precedence_over_include(self, tmp_path: Path) -> None:
        """If a path matches both include and exclude, exclude wins."""
        p = tmp_path / "notes" / "secret.md"
        p.parent.mkdir()
        p.write_text("x", encoding="utf-8")
        src = SourceEntry(
            name="x", type="markdown", path=str(tmp_path),
            include=("notes/**",),
            exclude=("**/secret.md",),
        )
        assert _path_passes_filters(p, tmp_path, src) is False

    def test_exclude_filename_pattern(self, tmp_path: Path) -> None:
        """``*.tmp`` matches against the bare filename."""
        p = tmp_path / "foo.tmp"
        p.write_text("x", encoding="utf-8")
        src = SourceEntry(
            name="x", type="markdown", path=str(tmp_path),
            exclude=("*.tmp",),
        )
        assert _path_passes_filters(p, tmp_path, src) is False


class TestRunIndexAppliesGlobs:
    def test_excludes_dropped_during_walk(self, tmp_path: Path) -> None:
        """Synthetic markdown source with files inside node_modules and a
        clean dir; exclude should keep only the clean files."""
        clean_dir = tmp_path / "notes"
        clean_dir.mkdir()
        nm_dir = tmp_path / "node_modules" / "pkg"
        nm_dir.mkdir(parents=True)
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        # Markdown ingestor needs MIN_CONTENT_LEN of content (30 chars).
        body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 3
        (clean_dir / "ok.md").write_text(body, encoding="utf-8")
        (nm_dir / "noise.md").write_text(body, encoding="utf-8")
        (git_dir / "log.md").write_text(body, encoding="utf-8")

        cfg = tmp_path / "sources.toml"
        db = tmp_path / "recall.db"
        root_str = str(tmp_path).replace("\\", "\\\\")
        cfg.write_text(
            f"schema_version = 1\n\n"
            f'[database]\npath = "{str(db).replace(chr(92), chr(92) + chr(92))}"\n\n'
            f'[[sources]]\nname = "notes"\ntype = "markdown"\n'
            f'path = "{root_str}"\nenabled = true\n'
            f'exclude = ["**/node_modules/**", "**/.git/**"]\n',
            encoding="utf-8",
        )

        rc = run_index(config_path=cfg, db_path=db)
        assert rc == 0

        conn = sqlite3.connect(str(db))
        try:
            # All indexed drawers' source_path should NOT contain
            # node_modules or .git.
            rows = conn.execute(
                "SELECT source_path FROM drawer_meta"
            ).fetchall()
            assert len(rows) >= 1
            for (sp,) in rows:
                assert "node_modules" not in sp, sp
                assert ".git" not in sp, sp
            # And clean file should be there.
            assert any("ok.md" in (sp or "") for (sp,) in rows)
        finally:
            conn.close()

    def test_include_allowlist_filters_to_match(self, tmp_path: Path) -> None:
        """Only files matching the include pattern get indexed."""
        ok_dir = tmp_path / "notes"
        ok_dir.mkdir()
        skip_dir = tmp_path / "drafts"
        skip_dir.mkdir()
        body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 3
        (ok_dir / "kept.md").write_text(body, encoding="utf-8")
        (skip_dir / "ignored.md").write_text(body, encoding="utf-8")

        cfg = tmp_path / "sources.toml"
        db = tmp_path / "recall.db"
        root_str = str(tmp_path).replace("\\", "\\\\")
        cfg.write_text(
            f"schema_version = 1\n\n"
            f'[database]\npath = "{str(db).replace(chr(92), chr(92) + chr(92))}"\n\n'
            f'[[sources]]\nname = "notes"\ntype = "markdown"\n'
            f'path = "{root_str}"\nenabled = true\n'
            f'include = ["notes/**"]\n',
            encoding="utf-8",
        )

        rc = run_index(config_path=cfg, db_path=db)
        assert rc == 0

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT source_path FROM drawer_meta"
            ).fetchall()
            for (sp,) in rows:
                assert "kept.md" in (sp or "")
                assert "ignored.md" not in (sp or "")
        finally:
            conn.close()

