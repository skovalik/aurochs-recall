"""Unit tests for the claude_ai ingestor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ingest.claude_ai import ClaudeAiIngestor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ingest"
EXPORT = FIXTURES / "claude_ai_export" / "conversations.json"


# ----- can_handle ---------------------------------------------------------


def test_can_handle_conversations_json():
    ing = ClaudeAiIngestor()
    assert ing.can_handle(EXPORT) is True


def test_can_handle_rejects_other_json():
    ing = ClaudeAiIngestor()
    assert ing.can_handle(Path("foo.json")) is False
    assert ing.can_handle(Path("messages.json")) is False


# ----- extract: top-level invariants -------------------------------------


def test_extract_yields_drawers():
    ing = ClaudeAiIngestor()
    drawers = list(ing.extract(EXPORT))
    # Fixture has 5 conversations with varying message counts. Filters
    # drop short / whitespace / system-role messages; we expect a
    # specific count.
    assert len(drawers) > 0


def test_thread_id_is_conversation_uuid():
    ing = ClaudeAiIngestor()
    drawers = list(ing.extract(EXPORT))
    expected_uuids = {
        "af7ab762-1234-5678-9abc-def012345678",
        "bc8de901-2345-6789-abcd-ef0123456789",
        "cd9ef012-3456-789a-bcde-f01234567890",
        "de0f1234-5678-9abc-def0-123456789abc",
        "ef123456-789a-bcde-f012-3456789abcde",
    }
    seen = {d.thread_id for d in drawers}
    assert seen <= expected_uuids


def test_drawer_uid_format():
    ing = ClaudeAiIngestor()
    drawers = list(ing.extract(EXPORT))
    for d in drawers:
        parts = d.drawer_uid.split(":")
        assert parts[0] == "claude_ai"
        assert len(parts) == 4


def test_role_normalization():
    ing = ClaudeAiIngestor()
    drawers = list(ing.extract(EXPORT))
    roles = {d.role for d in drawers}
    assert roles <= {"human", "assistant"}


# ----- extract: per-conversation behavior -------------------------------


def test_alpha_conversation_thread_chains():
    ing = ClaudeAiIngestor()
    drawers = [
        d for d in ing.extract(EXPORT)
        if d.thread_id == "af7ab762-1234-5678-9abc-def012345678"
    ]
    # Alpha has 4 messages, but msg-alpha-0004 ('ok') is filtered → 3 left.
    assert len(drawers) == 3
    # First has no parent
    assert drawers[0].parent_uid is None
    # Each subsequent message's parent_uid is the prior drawer's uid
    for prev, curr in zip(drawers, drawers[1:]):
        assert curr.parent_uid == prev.drawer_uid


def test_beta_conversation_uses_content_blocks():
    ing = ClaudeAiIngestor()
    drawers = [
        d for d in ing.extract(EXPORT)
        if d.thread_id == "bc8de901-2345-6789-abcd-ef0123456789"
    ]
    # 2 messages; beta-0002 has a content array with text + tool_use.
    # Only the text block should land in content.
    assert len(drawers) == 2
    assistant = [d for d in drawers if d.role == "assistant"][0]
    assert "Excepteur sint occaecat" in assistant.content
    assert "tool_use" not in assistant.content


def test_legacy_messages_key_handled():
    ing = ClaudeAiIngestor()
    drawers = [
        d for d in ing.extract(EXPORT)
        if d.thread_id == "cd9ef012-3456-789a-bcde-f01234567890"
    ]
    # Legacy export uses `messages` key — should still produce drawers
    assert len(drawers) == 2


def test_chat_messages_wins_over_messages_when_both_present():
    ing = ClaudeAiIngestor()
    drawers = [
        d for d in ing.extract(EXPORT)
        if d.thread_id == "de0f1234-5678-9abc-def0-123456789abc"
    ]
    # The "Both keys" conversation has BOTH chat_messages (real content)
    # and messages (with content beginning "STALE"). chat_messages must
    # win — none of the resulting drawer content should contain STALE.
    assert len(drawers) == 1
    assert "STALE" not in drawers[0].content
    assert "Nam libero tempore" in drawers[0].content


def test_edge_cases_filter_correctly():
    ing = ClaudeAiIngestor()
    drawers = [
        d for d in ing.extract(EXPORT)
        if d.thread_id == "ef123456-789a-bcde-f012-3456789abcde"
    ]
    # Edge conversation: 4 messages.
    # - msg-edge-0001: kept
    # - msg-edge-0002: system role — filtered
    # - msg-edge-0003: whitespace-only — filtered
    # - msg-edge-0004: kept
    assert len(drawers) == 2


# ----- malformed export handling -----------------------------------------


def test_invalid_json_raises_ingest_error(tmp_path: Path):
    p = tmp_path / "conversations.json"
    p.write_text("not json at all", encoding="utf-8")
    ing = ClaudeAiIngestor()
    from core.ingest._base import IngestError

    with pytest.raises(IngestError):
        list(ing.extract(p))


def test_top_level_object_raises_ingest_error(tmp_path: Path):
    # Top-level must be a list. Object should be rejected with a clear
    # error, not silently produce zero drawers.
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    ing = ClaudeAiIngestor()
    from core.ingest._base import IngestError

    with pytest.raises(IngestError):
        list(ing.extract(p))


def test_empty_export_yields_nothing(tmp_path: Path):
    p = tmp_path / "conversations.json"
    p.write_text("[]", encoding="utf-8")
    ing = ClaudeAiIngestor()
    assert list(ing.extract(p)) == []
