"""Tests for `recall status` --json output and the wal_size_pages row.

Plan v5 BLOCKER #4: status surfaces WAL size. Useful for debugging
wal-autocheckpoint behavior under high MCP burst.
"""
from __future__ import annotations

import json

from aurochs_recall.cli.main import main


# --------------------------------------------------------------------------
# Human-readable output: WAL pages row present
# --------------------------------------------------------------------------


def test_status_human_includes_wal_pages(fixture_db_path, capsys):
    """`recall status` (human output) shows the `WAL pages:` row."""
    code = main(["--db", str(fixture_db_path), "status"])
    assert code == 0
    out = capsys.readouterr().out
    # The PRAGMA may report 0 frames on a fresh fixture — what matters is
    # the row is present at all.
    assert "WAL pages:" in out


# --------------------------------------------------------------------------
# JSON output: wal_size_pages key + standard payload shape
# --------------------------------------------------------------------------


def test_status_json_basic_shape(fixture_db_path, capsys):
    """`recall status --json` returns a dict with the documented keys."""
    code = main(["--db", str(fixture_db_path), "status", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)

    # Required keys.
    for key in (
        "ok",
        "db",
        "db_size_bytes",
        "schema_version",
        "schema_applied_at",
        "wal_size_pages",
        "drawers_total",
        "drawers_by_source",
        "last_indexed_at",
        "ingest_errors",
    ):
        assert key in payload, f"missing key: {key}"

    assert payload["ok"] is True
    assert isinstance(payload["db_size_bytes"], int)
    assert isinstance(payload["drawers_total"], int)
    assert isinstance(payload["drawers_by_source"], dict)
    assert payload["drawers_total"] >= 1


def test_status_json_wal_size_pages_is_int(fixture_db_path, capsys):
    """wal_size_pages is an integer (PRAGMA wal_checkpoint frame count).

    PRAGMA wal_checkpoint returns -1 when the DB is not in WAL mode;
    that's a valid value to surface (operator can spot it and react).
    """
    code = main(["--db", str(fixture_db_path), "status", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    # The key must be present and an int on a healthy DB. Negative is
    # accepted (sqlite signals "not in WAL mode" with -1).
    assert payload["wal_size_pages"] is not None
    assert isinstance(payload["wal_size_pages"], int)


def test_status_json_schema_version_int(fixture_db_path, capsys):
    """schema_version is an int when migrations are applied."""
    code = main(["--db", str(fixture_db_path), "status", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] is not None
    assert isinstance(payload["schema_version"], int)
    assert payload["schema_version"] >= 1


def test_status_json_drawers_by_source_is_serializable(fixture_db_path, capsys):
    """drawers_by_source maps source-id -> int count, always JSON-safe."""
    code = main(["--db", str(fixture_db_path), "status", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    for source, count in payload["drawers_by_source"].items():
        assert isinstance(source, str)
        assert isinstance(count, int)
        assert count >= 0


# --------------------------------------------------------------------------
# Missing DB: --json reports structured error, exit 1
# --------------------------------------------------------------------------


def test_status_json_missing_db_returns_error_payload(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "nope.db"), "status", "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "db_not_found"
    assert "nope.db" in payload["db"]
