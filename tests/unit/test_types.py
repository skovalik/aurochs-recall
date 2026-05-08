"""Unit tests for core.types — Drawer construction + identity primitives."""

from __future__ import annotations

import pytest

from core.types import (
    HASH_INPUT_VERSION,
    Drawer,
    Entity,
    Hit,
    Relationship,
    compute_content_hash,
    compute_drawer_uid,
    normalize_whitespace,
)


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

class TestNormalizeWhitespace:
    def test_collapses_runs(self) -> None:
        assert normalize_whitespace("a  b\t\tc\n\nd") == "a b c d"

    def test_strips_ends(self) -> None:
        assert normalize_whitespace("   hello   ") == "hello"

    def test_idempotent(self) -> None:
        once = normalize_whitespace("foo  bar\nbaz")
        assert normalize_whitespace(once) == once

    def test_empty(self) -> None:
        assert normalize_whitespace("") == ""
        assert normalize_whitespace("   \n\t  ") == ""

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            normalize_whitespace(b"bytes-not-str")  # type: ignore[arg-type]


class TestComputeContentHash:
    def test_deterministic(self) -> None:
        h1 = compute_content_hash("human", "hello world")
        h2 = compute_content_hash("human", "hello world")
        assert h1 == h2

    def test_role_in_hash(self) -> None:
        # Same content, different role → different hash. This is the
        # whole point of putting role in the hash input.
        h_human = compute_content_hash("human", "hi")
        h_assistant = compute_content_hash("assistant", "hi")
        assert h_human != h_assistant

    def test_whitespace_invariant(self) -> None:
        # The whole point of normalize_whitespace in the hash input.
        h_normal = compute_content_hash("human", "hello world")
        h_extra_ws = compute_content_hash("human", "hello   world")
        h_newlines = compute_content_hash("human", "hello\n\nworld")
        assert h_normal == h_extra_ws == h_newlines

    def test_returns_64_hex(self) -> None:
        h = compute_content_hash("human", "anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestComputeDrawerUid:
    def test_format(self) -> None:
        # 64-char sha256 hex; uid takes first 12.
        h = "a" * 64
        uid = compute_drawer_uid("claude_code", "session-x:0", h)
        assert uid == "claude_code:session-x:0:aaaaaaaaaaaa"

    def test_rejects_short_hash(self) -> None:
        with pytest.raises(ValueError):
            compute_drawer_uid("claude_code", "abc", "short")

    def test_rejects_empty_components(self) -> None:
        h = "a" * 64
        with pytest.raises(ValueError):
            compute_drawer_uid("", "abc", h)
        with pytest.raises(ValueError):
            compute_drawer_uid("claude_code", "", h)
        with pytest.raises(ValueError):
            compute_drawer_uid("claude_code", "abc", "")


# ---------------------------------------------------------------------------
# Drawer dataclass
# ---------------------------------------------------------------------------

class TestDrawer:
    def test_minimal_construction(self) -> None:
        d = Drawer(
            source="claude_code",
            source_id="session-1:0",
            role="human",
            content="hello",
            created_at=1700_000_000,
        )
        assert d.role == "human"
        assert d.content == "hello"
        assert d.content_hash != ""  # auto-derived
        assert d.drawer_uid.startswith("claude_code:session-1:0:")
        assert len(d.drawer_uid.split(":")[-1]) == 12
        assert d.hash_input_version == HASH_INPUT_VERSION

    def test_uid_uniqueness_for_different_content(self) -> None:
        d1 = Drawer(
            source="s", source_id="i", role="human",
            content="hello", created_at=0,
        )
        d2 = Drawer(
            source="s", source_id="i", role="human",
            content="goodbye", created_at=0,
        )
        assert d1.drawer_uid != d2.drawer_uid

    def test_uid_stable_across_whitespace(self) -> None:
        # Same content modulo whitespace → same uid (because hash is
        # whitespace-normalized).
        d1 = Drawer(
            source="s", source_id="i", role="human",
            content="hello world", created_at=0,
        )
        d2 = Drawer(
            source="s", source_id="i", role="human",
            content="hello   world\n", created_at=0,
        )
        assert d1.drawer_uid == d2.drawer_uid

    def test_uid_changes_with_role(self) -> None:
        d_human = Drawer(
            source="s", source_id="i", role="human",
            content="hi", created_at=0,
        )
        d_assistant = Drawer(
            source="s", source_id="i", role="assistant",
            content="hi", created_at=0,
        )
        assert d_human.drawer_uid != d_assistant.drawer_uid

    def test_explicit_hash_preserved(self) -> None:
        # If the caller pre-computes the hash, we trust it.
        h = compute_content_hash("human", "hello")
        d = Drawer(
            source="s", source_id="i", role="human",
            content="hello", created_at=0, content_hash=h,
        )
        assert d.content_hash == h
        assert d.drawer_uid == compute_drawer_uid("s", "i", h)

    def test_frozen(self) -> None:
        d = Drawer(
            source="s", source_id="i", role="human",
            content="x", created_at=0,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            d.content = "y"  # type: ignore[misc]

    def test_rejects_empty_role(self) -> None:
        with pytest.raises(ValueError):
            Drawer(
                source="s", source_id="i", role="",
                content="x", created_at=0,
            )

    def test_rejects_empty_source(self) -> None:
        with pytest.raises(ValueError):
            Drawer(
                source="", source_id="i", role="human",
                content="x", created_at=0,
            )

    def test_rejects_out_of_range_risk(self) -> None:
        with pytest.raises(ValueError):
            Drawer(
                source="s", source_id="i", role="human",
                content="x", created_at=0, risk_score=101,
            )
        with pytest.raises(ValueError):
            Drawer(
                source="s", source_id="i", role="human",
                content="x", created_at=0, risk_score=-1,
            )


# ---------------------------------------------------------------------------
# Other dataclasses (smoke tests)
# ---------------------------------------------------------------------------

class TestHit:
    def test_construction(self) -> None:
        h = Hit(
            drawer_uid="s:i:abcdef012345",
            score=0.5,
            snippet="hello",
            source="s",
            created_at=0,
            rank=1,
        )
        assert h.rank == 1


class TestEntity:
    def test_defaults(self) -> None:
        e = Entity(id=1, name="Stefan", type="person")
        assert e.metadata == {}
        assert e.source == "seed"


class TestRelationship:
    def test_defaults(self) -> None:
        r = Relationship(id=1, subject_id=1, predicate="MENTIONS", object_id=2)
        assert r.valid_to is None
        assert r.drawer_uid is None
