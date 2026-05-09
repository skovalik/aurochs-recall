"""Tests for the MCP server tool implementations.

The MCP layer is a thin shim over the CLI/core APIs. We exercise the
pure-Python `_do_*` functions (no FastMCP runtime needed) and verify:

  - All 5 tools are registered when build_server() runs.
  - Each tool's payload shape matches the documented contract.
  - Error paths return structured error payloads, not exceptions.
"""
from __future__ import annotations

import sqlite3

import pytest

from aurochs_recall.mcp.server import (
    _do_recall_drawer,
    _do_recall_forget,
    _do_recall_graph_query,
    _do_recall_search,
    _do_recall_status,
    build_server,
)


# --------------------------------------------------------------------------
# Tool registration: all 5 tools present
# --------------------------------------------------------------------------


def test_build_server_registers_all_five_tools():
    """Plan v5 spec: 5 MCP tools shipped with the [mcp] extra."""
    server = build_server()
    # FastMCP exposes a tool registry; inspect via list_tools.
    # The exact API depends on mcp package version, so we use a defensive
    # introspection approach.
    tool_names = _list_tool_names(server)
    expected = {
        "recall_search",
        "recall_drawer",
        "recall_status",
        "recall_graph_query",
        "recall_forget",
    }
    assert expected.issubset(set(tool_names)), (
        f"missing MCP tools: {expected - set(tool_names)}; "
        f"registered: {tool_names}"
    )


def _list_tool_names(server) -> list[str]:
    """Best-effort tool-name extraction across FastMCP versions."""
    # Try the common attributes.
    for attr in ("_tool_manager", "tools", "_tools"):
        tm = getattr(server, attr, None)
        if tm is not None:
            for sub in ("_tools", "tools"):
                inner = getattr(tm, sub, None)
                if isinstance(inner, dict):
                    return list(inner.keys())
    # Fallback: inspect __dict__.
    return []


# --------------------------------------------------------------------------
# recall_search
# --------------------------------------------------------------------------


def test_do_recall_search_returns_hits(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    payload = _do_recall_search("mehrwerk", top_k=5)
    assert payload["ok"] is True
    assert payload["query"] == "mehrwerk"
    assert payload["top_k"] == 5
    assert isinstance(payload["hits"], list)
    if payload["hits"]:
        h = payload["hits"][0]
        for k in ("drawer_uid", "score", "rank", "source", "snippet"):
            assert k in h


def test_do_recall_search_missing_db_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(tmp_path / "nope.db"))
    payload = _do_recall_search("anything")
    assert payload["ok"] is False
    assert payload["error"] == "db_not_found"
    assert payload["hits"] == []


# --------------------------------------------------------------------------
# recall_drawer
# --------------------------------------------------------------------------


def test_do_recall_drawer_returns_full_drawer(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    conn = sqlite3.connect(str(fixture_db_path))
    conn.row_factory = sqlite3.Row
    try:
        full_uid = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    payload = _do_recall_drawer(full_uid)
    assert payload["ok"] is True
    assert payload["drawer_uid"] == full_uid
    for k in (
        "source",
        "source_id",
        "role",
        "created_at",
        "content_hash",
        "content",
    ):
        assert k in payload


def test_do_recall_drawer_unknown_returns_error(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    payload = _do_recall_drawer("zzz_no_such_drawer")
    assert payload["ok"] is False
    assert payload["error"] == "drawer_not_found"


def test_do_recall_drawer_ambiguous_returns_candidates(
    fixture_db_path, monkeypatch
):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    # Find an ambiguous 1-char hash prefix.
    conn = sqlite3.connect(str(fixture_db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 50"
        ).fetchall()
        first_chars: dict[str, int] = {}
        for r in rows:
            ch = r["drawer_uid"].rsplit(":", 1)[1][0]
            first_chars[ch] = first_chars.get(ch, 0) + 1
        ambiguous = next((ch for ch, n in first_chars.items() if n > 1), None)
    finally:
        conn.close()

    if ambiguous is None:
        pytest.skip("fixture too small for ambiguity test")

    payload = _do_recall_drawer(ambiguous)
    assert payload["ok"] is False
    assert payload["error"] == "ambiguous_prefix"
    assert len(payload["candidates"]) >= 2


# --------------------------------------------------------------------------
# recall_status
# --------------------------------------------------------------------------


def test_do_recall_status_payload_shape(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    payload = _do_recall_status()
    assert payload["ok"] is True
    for k in (
        "db",
        "db_size_bytes",
        "schema_version",
        "wal_size_pages",
        "drawers_total",
        "drawers_by_source",
    ):
        assert k in payload


def test_do_recall_status_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(tmp_path / "nope.db"))
    payload = _do_recall_status()
    assert payload["ok"] is False
    assert payload["error"] == "db_not_found"


# --------------------------------------------------------------------------
# recall_graph_query
# --------------------------------------------------------------------------


def test_do_recall_graph_query_unknown_entity(fixture_db_path, monkeypatch):
    """Unknown entity returns ok=True with empty matches list."""
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    payload = _do_recall_graph_query("DefinitelyNotAnEntity")
    assert payload["ok"] is True
    assert payload["matches"] == []


# --------------------------------------------------------------------------
# recall_forget — covered more deeply in test_cli_forget;
# here we just verify the MCP wrapper round-trip.
# --------------------------------------------------------------------------


def test_do_recall_forget_dry_run(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    conn = sqlite3.connect(str(fixture_db_path))
    conn.row_factory = sqlite3.Row
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    payload = _do_recall_forget(full, dry_run=True)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["drawer_uid"] == full


def test_do_recall_forget_real_hide(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    conn = sqlite3.connect(str(fixture_db_path))
    conn.row_factory = sqlite3.Row
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    payload = _do_recall_forget(full, dry_run=False, reason="test")
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["drawer_uid"] == full
    assert payload["reason"] == "test"

    # Verify it actually wrote.
    conn = sqlite3.connect(str(fixture_db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT drawer_uid, reason FROM hidden_drawers WHERE drawer_uid = ?",
            (full,),
        ).fetchone()
        assert row is not None
        assert row["reason"] == "test"
    finally:
        conn.close()


def test_do_recall_forget_unknown_returns_error(fixture_db_path, monkeypatch):
    monkeypatch.setenv("RECALL_DB", str(fixture_db_path))
    payload = _do_recall_forget("zzz_no_such_drawer")
    assert payload["ok"] is False
    assert payload["error"] == "drawer_not_found"
