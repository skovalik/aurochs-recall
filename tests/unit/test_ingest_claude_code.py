"""Unit tests for the claude_code ingestor."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.ingest.claude_code import ClaudeCodeIngestor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ingest"
FLAT_DIR = FIXTURES / "claude_code_flat"
NESTED_SESSION = (
    FIXTURES / "claude_code_nested" / "3a8f5c1e-9b2d-4a7e-b6f1-2c5d8e9a0b3c"
)
FLAT_GOOD = FLAT_DIR / "7d3e2a1b-6c4f-4d5e-9a8b-1f2c3d4e5f60.jsonl"
FLAT_BAD = FLAT_DIR / "badd1ad0-bad0-4bad-bad0-baddd1ad1ad0.jsonl"
FLAT_MODERN = FLAT_DIR / "0d34a01e-aaaa-bbbb-cccc-dddddddddddd.jsonl"
NESTED_MAIN = NESTED_SESSION / "main.jsonl"
NESTED_AGENT = NESTED_SESSION / "subagents" / "agent-a001.jsonl"


# ----- can_handle ---------------------------------------------------------


def test_can_handle_flat_uuid_filename():
    ing = ClaudeCodeIngestor()
    assert ing.can_handle(FLAT_GOOD) is True


def test_can_handle_nested_main():
    ing = ClaudeCodeIngestor()
    assert ing.can_handle(NESTED_MAIN) is True


def test_can_handle_nested_subagent():
    ing = ClaudeCodeIngestor()
    assert ing.can_handle(NESTED_AGENT) is True


def test_can_handle_rejects_non_uuid_jsonl():
    ing = ClaudeCodeIngestor()
    assert ing.can_handle(Path("foo.jsonl")) is False


def test_can_handle_rejects_non_jsonl():
    ing = ClaudeCodeIngestor()
    assert (
        ing.can_handle(Path("7d3e2a1b-6c4f-4d5e-9a8b-1f2c3d4e5f60.json"))
        is False
    )


# ----- extract: flat layout ----------------------------------------------


def test_extract_flat_yields_expected_count():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    # Fixture has 7 records, but 'ok' (too short) and '/screenshot' (slash-only)
    # are filtered, plus a tool_use block is dropped from msg-0004 leaving
    # only its text. So we expect 5 valid drawers.
    assert len(drawers) == 5


def test_extract_flat_threads_parents_correctly():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    # First drawer has no parent
    assert drawers[0].parent_uid is None
    # Each subsequent drawer's parent_uid matches the prior drawer's uid
    for prev, curr in zip(drawers, drawers[1:]):
        assert curr.parent_uid == prev.drawer_uid


def test_extract_flat_session_uuid_is_thread_id():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    expected_uuid = "7d3e2a1b-6c4f-4d5e-9a8b-1f2c3d4e5f60"
    for d in drawers:
        assert d.thread_id == expected_uuid


def test_extract_flat_role_normalization():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    roles = {d.role for d in drawers}
    assert roles <= {"human", "assistant"}


def test_extract_flat_position_is_sequential():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    positions = [d.position_in_thread for d in drawers]
    # positions are 0-based and contiguous (filtered records don't advance)
    assert positions == list(range(len(drawers)))


def test_extract_flat_drawer_uid_format():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    for d in drawers:
        # Expected: claude_code:<uuid>:<position>:<hash12>
        parts = d.drawer_uid.split(":")
        assert parts[0] == "claude_code"
        assert len(parts) == 4  # source : uuid : position : hash12


def test_extract_flat_drops_tool_use_blocks():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    # msg-0004 had a tool_use block alongside text — only the text should
    # land in the drawer content.
    msg4 = [
        d for d in drawers
        if d.metadata.get("record_uuid") == "msg-0004"
    ]
    assert len(msg4) == 1
    assert "tool_use" not in msg4[0].content
    assert "Sed do eiusmod" in msg4[0].content


# ----- extract: bad-line handling ----------------------------------------


def test_extract_bad_line_skips_and_logs(caplog):
    ing = ClaudeCodeIngestor()
    with caplog.at_level(logging.WARNING, logger="core.ingest.claude_code"):
        drawers = list(ing.extract(FLAT_BAD))
    # 4 lines in the file: 3 are valid JSON. All 3 should yield drawers.
    assert len(drawers) == 3
    # The bad-line warning should appear in the log
    assert any("bad jsonl" in r.message for r in caplog.records)


# ----- extract: modern wrapped schema -----------------------------------


def test_extract_modern_schema_unwraps_message():
    """The current Claude Code schema wraps role + content inside a
    nested ``message`` object. Older / hand-rolled jsonl puts them at
    the top level. The ingestor must handle both."""
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_MODERN))
    # Fixture: 4 records. modern-0003 is type='tool_result' (filtered).
    # The other 3 produce drawers.
    assert len(drawers) == 3
    assert drawers[0].role == "human"
    assert drawers[1].role == "assistant"
    assert "Lorem ipsum" in drawers[0].content
    assert "Sed do eiusmod" in drawers[1].content


def test_extract_modern_schema_captures_metadata():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_MODERN))
    # The assistant drawers should have the model captured from
    # message.model (modern schema), not the missing top-level model.
    assistants = [d for d in drawers if d.role == "assistant"]
    for d in assistants:
        assert d.metadata.get("model") == "claude-sonnet-test"
    # All drawers should have claude_code_version stamped from outer "version"
    for d in drawers:
        if "claude_code_version" in d.metadata:
            assert d.metadata["claude_code_version"] == "1.2.3"


def test_extract_modern_schema_drops_tool_result():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_MODERN))
    # tool_result is filtered — none of the resulting drawers should
    # contain the literal tool output.
    for d in drawers:
        assert "tool output that should be filtered" not in d.content


# ----- extract: nested layout --------------------------------------------


def test_extract_nested_main():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(NESTED_MAIN))
    assert len(drawers) == 4
    # All drawers share the session UUID from the parent directory name
    uuid = "3a8f5c1e-9b2d-4a7e-b6f1-2c5d8e9a0b3c"
    for d in drawers:
        assert d.thread_id == uuid


def test_extract_nested_subagent_session_uuid_resolves():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(NESTED_AGENT))
    # subagent jsonl is two levels deep: <uuid>/subagents/agent-a001.jsonl
    # session UUID resolves from the grandparent.
    uuid = "3a8f5c1e-9b2d-4a7e-b6f1-2c5d8e9a0b3c"
    for d in drawers:
        assert d.thread_id == uuid


def test_extract_nested_subagent_metadata_tags_filename():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(NESTED_AGENT))
    for d in drawers:
        assert d.metadata.get("subagent_file") == "agent-a001"


# ----- extract: source_path is absolute ----------------------------------


def test_source_path_is_absolute():
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(FLAT_GOOD))
    for d in drawers:
        assert Path(d.source_path).is_absolute()


# ----- extract: created_at falls back to mtime ---------------------------


def test_created_at_falls_back_to_mtime(tmp_path: Path):
    # Build a tiny jsonl with no timestamps in the records.
    fname = "7d3e2a1b-1111-2222-3333-444455556666.jsonl"
    p = tmp_path / fname
    p.write_text(
        '{"role":"human","content":"Lorem ipsum dolor sit amet, consectetur adipiscing elit and a bit more."}\n',
        encoding="utf-8",
    )
    mtime = int(p.stat().st_mtime)
    ing = ClaudeCodeIngestor()
    drawers = list(ing.extract(p))
    assert len(drawers) == 1
    assert drawers[0].created_at == mtime
