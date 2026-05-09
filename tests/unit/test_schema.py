"""Unit tests for schema application + connection pragmas."""

from __future__ import annotations

from pathlib import Path

import pytest

from aurochs_recall.core.db import db_connect
from aurochs_recall.core.schema import CURRENT_SCHEMA_VERSION, apply_schema, current_schema_version


def test_apply_schema_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        apply_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # Spine-required tables
        for required in (
            "drawer_meta",
            "entities",
            "entity_types",
            "type_aliases",
            "relationships",
            "predicates",
            "schema_version",
            "index_state",
            "ingest_errors",
        ):
            assert required in tables, f"missing table: {required}"

        # FTS5 virtual table is reported separately
        fts = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='drawers_fts'"
        ).fetchone()
        assert fts is not None
    finally:
        conn.close()


def test_schema_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        apply_schema(conn)
        # Apply again — should not raise.
        apply_schema(conn)

        version = current_schema_version(conn)
        assert version == CURRENT_SCHEMA_VERSION

        rows = conn.execute(
            "SELECT version FROM schema_version WHERE status='applied'"
        ).fetchall()
        # apply_schema is cumulative (v1 + v2 + ...), and ``OR IGNORE`` on
        # re-apply keeps exactly one row per applied version. Total row
        # count therefore equals the current version.
        assert len(rows) == CURRENT_SCHEMA_VERSION
        applied_versions = sorted(int(r["version"]) for r in rows)
        assert applied_versions == list(range(1, CURRENT_SCHEMA_VERSION + 1))
    finally:
        conn.close()


def test_seed_data_loaded(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        apply_schema(conn)

        types = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM entity_types"
            ).fetchall()
        }
        # All seven seed types from the SQL
        for expected in (
            "person", "project", "concept", "event",
            "tool", "methodology", "place",
        ):
            assert expected in types

        predicates = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM predicates"
            ).fetchall()
        }
        assert "MENTIONS" in predicates
        assert "RELATED_TO" in predicates
        assert len(predicates) >= 10
    finally:
        conn.close()


def test_foreign_keys_pragma_on(tmp_path: Path) -> None:
    """The whole point of plan v4 fix #2 — FKs must be enforced."""
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert result == 1, "PRAGMA foreign_keys must be ON for every connection"
    finally:
        conn.close()


def test_journal_mode_wal(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        result = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert result.lower() == "wal"
    finally:
        conn.close()


def test_busy_timeout_set(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        result = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert result == 30000
    finally:
        conn.close()


def test_fk_actually_enforces(tmp_path: Path) -> None:
    """Smoke-test that FK enforcement is more than a pragma name."""
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        apply_schema(conn)
        # entities.type FK must reference entity_types — inserting an
        # unknown type must fail.
        with pytest.raises(Exception):  # IntegrityError
            conn.execute(
                "INSERT INTO entities (name, type, first_seen, last_seen, source) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Test", "nonexistent_type", 0, 0, "seed"),
            )
    finally:
        conn.close()


def test_check_constraint_drawer_uid_nonempty(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        apply_schema(conn)
        with pytest.raises(Exception):  # IntegrityError from CHECK
            conn.execute(
                "INSERT INTO drawer_meta (drawer_uid, source, source_id, role, "
                "created_at, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
                ("", "claude_code", "abc", "human", 0, "x" * 64),
            )
    finally:
        conn.close()


def test_check_constraint_risk_score_range(tmp_path: Path) -> None:
    db = tmp_path / "recall.db"
    conn = db_connect(db)
    try:
        apply_schema(conn)
        with pytest.raises(Exception):  # IntegrityError from CHECK
            conn.execute(
                "INSERT INTO drawer_meta (drawer_uid, source, source_id, role, "
                "created_at, content_hash, risk_score) VALUES "
                "(?, ?, ?, ?, ?, ?, ?)",
                ("u:1:abc", "s", "1", "human", 0, "x" * 64, 200),
            )
    finally:
        conn.close()
