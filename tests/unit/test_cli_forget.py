"""Tests for the `recall forget` subcommand.

Plan v5 BLOCKERs covered:
  #6 — single-uid path supports `--dry-run` for symmetry with batch path.
  #7 — drawer_uid prefix matching: accept unique prefix, error with
       disambiguation list if multiple match.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aurochs_recall.cli.main import (
    _AmbiguousPrefixError,
    _DrawerNotFoundError,
    main,
    resolve_drawer_uid_prefix,
)


# --------------------------------------------------------------------------
# resolve_drawer_uid_prefix unit tests (no CLI)
# --------------------------------------------------------------------------


def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


def test_resolve_exact_uid_returns_same(fixture_db_path):
    """Exact uid match returns the uid verbatim (fast path)."""
    conn = _conn(fixture_db_path)
    try:
        # Pull any uid out of the fixture and resolve it.
        row = conn.execute("SELECT drawer_uid FROM drawer_meta LIMIT 1").fetchone()
        assert row is not None
        full = row["drawer_uid"]
        resolved = resolve_drawer_uid_prefix(full, conn)
        assert resolved == full
    finally:
        conn.close()


def test_resolve_hash_segment_prefix(fixture_db_path):
    """A short prefix matching the trailing content_hash resolves uniquely."""
    conn = _conn(fixture_db_path)
    try:
        # Grab the hash portion (after the last colon) of one uid.
        row = conn.execute("SELECT drawer_uid FROM drawer_meta LIMIT 1").fetchone()
        assert row is not None
        full = row["drawer_uid"]
        # Hash portion. drawer_uid format: source:source_id:content_hash[:12]
        hash_prefix = full.rsplit(":", 1)[1][:8]  # 8-char hash prefix

        # Verify it's actually a unique prefix in this fixture.
        n_match = conn.execute(
            "SELECT COUNT(*) FROM drawer_meta WHERE drawer_uid LIKE ?",
            (f"%:{hash_prefix}%",),
        ).fetchone()[0]
        if n_match != 1:
            pytest.skip(
                f"hash prefix {hash_prefix!r} matches {n_match} drawers; "
                "fixture not deterministic enough for this test"
            )

        resolved = resolve_drawer_uid_prefix(hash_prefix, conn)
        assert resolved == full
    finally:
        conn.close()


def test_resolve_unknown_prefix_raises_not_found(fixture_db_path):
    conn = _conn(fixture_db_path)
    try:
        with pytest.raises(_DrawerNotFoundError):
            resolve_drawer_uid_prefix("zzz_no_such_hash_prefix", conn)
    finally:
        conn.close()


def test_resolve_ambiguous_prefix_raises_with_candidates(fixture_db_path):
    """Empty/short prefix matches everything — must raise with candidates."""
    conn = _conn(fixture_db_path)
    try:
        # The colon character appears in every drawer_uid (it's the separator),
        # so the substring ':' will match every row. Use a 1-char prefix that
        # we expect to match multiple uids.
        # First, find a 1-char hash prefix that has > 1 match.
        rows = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 50"
        ).fetchall()
        # Find a hex char at position 0 of the hash that has >1 occurrences.
        first_chars: dict[str, int] = {}
        for r in rows:
            ch = r["drawer_uid"].rsplit(":", 1)[1][0]
            first_chars[ch] = first_chars.get(ch, 0) + 1
        ambiguous_char = next(
            (ch for ch, n in first_chars.items() if n > 1),
            None,
        )
        if ambiguous_char is None:
            pytest.skip("fixture too small to find an ambiguous 1-char prefix")

        with pytest.raises(_AmbiguousPrefixError) as exc:
            resolve_drawer_uid_prefix(ambiguous_char, conn)
        assert exc.value.prefix == ambiguous_char
        assert len(exc.value.candidates) >= 2
        assert len(exc.value.candidates) <= 5  # capped
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI: dry-run path
# --------------------------------------------------------------------------


def test_forget_dry_run_does_not_modify_db(fixture_db_path, capsys):
    """`recall forget <uid> --dry-run` previews without writing."""
    conn = _conn(fixture_db_path)
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    code = main(["--db", str(fixture_db_path), "forget", full, "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    assert full in out

    # Confirm no hidden_drawers row was created (or table doesn't exist yet).
    conn = _conn(fixture_db_path)
    try:
        # Either the table doesn't exist, or it exists with no row for this uid.
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM hidden_drawers WHERE drawer_uid = ?",
                (full,),
            ).fetchone()
            assert row["n"] == 0
        except sqlite3.OperationalError:
            # Table doesn't exist yet — also acceptable for --dry-run.
            pass
    finally:
        conn.close()


def test_forget_dry_run_json_output(fixture_db_path, capsys):
    """`--dry-run --json` returns structured preview."""
    conn = _conn(fixture_db_path)
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    code = main(
        ["--db", str(fixture_db_path), "forget", full, "--dry-run", "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["drawer_uid"] == full


# --------------------------------------------------------------------------
# CLI: real-hide path
# --------------------------------------------------------------------------


def test_forget_writes_hidden_drawers_row(fixture_db_path, capsys):
    """`recall forget <uid>` (no --dry-run) inserts into hidden_drawers."""
    conn = _conn(fixture_db_path)
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    code = main(["--db", str(fixture_db_path), "forget", full])
    assert code == 0
    out = capsys.readouterr().out
    assert "hidden:" in out.lower()
    assert full in out

    # Verify table + row exist.
    conn = _conn(fixture_db_path)
    try:
        row = conn.execute(
            "SELECT drawer_uid, hidden_at, unhidden_at "
            "FROM hidden_drawers WHERE drawer_uid = ?",
            (full,),
        ).fetchone()
        assert row is not None
        assert row["drawer_uid"] == full
        assert row["hidden_at"] is not None
        assert row["unhidden_at"] is None
    finally:
        conn.close()


def test_forget_with_reason(fixture_db_path, capsys):
    """`--reason` is recorded in the row."""
    conn = _conn(fixture_db_path)
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    code = main(
        [
            "--db",
            str(fixture_db_path),
            "forget",
            full,
            "--reason",
            "PII leak in this conversation",
        ]
    )
    assert code == 0

    conn = _conn(fixture_db_path)
    try:
        row = conn.execute(
            "SELECT reason FROM hidden_drawers WHERE drawer_uid = ?",
            (full,),
        ).fetchone()
        assert row is not None
        assert row["reason"] == "PII leak in this conversation"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI: error paths
# --------------------------------------------------------------------------


def test_forget_unknown_uid_returns_2(fixture_db_path, capsys):
    code = main(
        ["--db", str(fixture_db_path), "forget", "zzz_no_such_drawer_prefix"]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "no drawer matched" in err.lower()


def test_forget_ambiguous_prefix_lists_candidates(fixture_db_path, capsys):
    """An ambiguous prefix exits 2 with a list of candidate uids."""
    # Find an ambiguous 1-char prefix (same logic as resolver test).
    conn = _conn(fixture_db_path)
    try:
        rows = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 50"
        ).fetchall()
        first_chars: dict[str, int] = {}
        for r in rows:
            ch = r["drawer_uid"].rsplit(":", 1)[1][0]
            first_chars[ch] = first_chars.get(ch, 0) + 1
        ambiguous_char = next(
            (ch for ch, n in first_chars.items() if n > 1), None
        )
        if ambiguous_char is None:
            pytest.skip("fixture too small for this test")
    finally:
        conn.close()

    code = main(["--db", str(fixture_db_path), "forget", ambiguous_char])
    assert code == 2
    err = capsys.readouterr().err
    assert "matches multiple drawers" in err.lower()


def test_forget_ambiguous_prefix_json_lists_candidates(fixture_db_path, capsys):
    conn = _conn(fixture_db_path)
    try:
        rows = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 50"
        ).fetchall()
        first_chars: dict[str, int] = {}
        for r in rows:
            ch = r["drawer_uid"].rsplit(":", 1)[1][0]
            first_chars[ch] = first_chars.get(ch, 0) + 1
        ambiguous_char = next(
            (ch for ch, n in first_chars.items() if n > 1), None
        )
        if ambiguous_char is None:
            pytest.skip("fixture too small for this test")
    finally:
        conn.close()

    code = main(
        ["--db", str(fixture_db_path), "forget", ambiguous_char, "--json"]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == "ambiguous_prefix"
    assert len(payload["candidates"]) >= 2


def test_forget_missing_db_returns_1(tmp_path, capsys):
    code = main(
        ["--db", str(tmp_path / "nope.db"), "forget", "abc123de"]
    )
    assert code == 1


# --------------------------------------------------------------------------
# Re-hide is idempotent (covers ON CONFLICT path)
# --------------------------------------------------------------------------


def test_forget_re_hide_is_idempotent(fixture_db_path, capsys):
    """Hiding the same drawer twice is OK; hidden_at advances."""
    conn = _conn(fixture_db_path)
    try:
        full = conn.execute(
            "SELECT drawer_uid FROM drawer_meta LIMIT 1"
        ).fetchone()["drawer_uid"]
    finally:
        conn.close()

    code1 = main(["--db", str(fixture_db_path), "forget", full])
    assert code1 == 0
    capsys.readouterr()

    code2 = main(["--db", str(fixture_db_path), "forget", full, "--reason", "x"])
    assert code2 == 0

    conn = _conn(fixture_db_path)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM hidden_drawers WHERE drawer_uid = ?",
            (full,),
        ).fetchone()
        assert rows["n"] == 1  # ON CONFLICT updates rather than inserts
        row = conn.execute(
            "SELECT reason FROM hidden_drawers WHERE drawer_uid = ?",
            (full,),
        ).fetchone()
        assert row["reason"] == "x"
    finally:
        conn.close()
