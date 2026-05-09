"""Searcher unit tests — snippet generation, ANSI bold, NO_COLOR/non-tty."""
from __future__ import annotations

import os
import re

import pytest

from aurochs_recall.core.search import (
    ANSI_BOLD_CLOSE,
    ANSI_BOLD_OPEN,
    Searcher,
    _extract_match_terms,
    _first_match_index,
)


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_search_default_mode(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        hits = s.search("mehrwerk")
        assert hits
        assert all(h.snippet for h in hits)


def test_search_returns_drawers_alongside(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        hits = s.search("mehrwerk", limit=3)
        assert len(s.last_drawers) == len(hits)
        for hit, drawer in zip(hits, s.last_drawers):
            assert hit.drawer_uid == drawer.drawer_uid


def test_unsupported_mode_raises(fixture_db_path):
    with Searcher(db_path=fixture_db_path) as s:
        with pytest.raises(ValueError, match="Unsupported search mode"):
            s.search("x", mode="hybrid")  # type: ignore[arg-type]


def test_snippet_truncates_when_long(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        hits = s.search("lorem", full=False)
        for h in hits:
            # Default snippet is single-line, capped near SNIPPET_MAX_LEN.
            assert "\n" not in h.snippet
            assert len(h.snippet) <= 220  # SNIPPET_MAX_LEN + ellipses


def test_snippet_full_mode_preserves_content(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        hits = s.search("recall architecture", full=True)
        # In full mode the snippet should contain the entire content of the
        # matching drawer (including newlines for the markdown entries).
        assert any("Four layers" in h.snippet for h in hits)


def test_ansi_bolding_with_color(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=True) as s:
        hits = s.search("mehrwerk", limit=1)
        assert hits
        # Snippet should contain ANSI bold around the matched term.
        assert ANSI_BOLD_OPEN in hits[0].snippet
        assert ANSI_BOLD_CLOSE in hits[0].snippet


def test_no_color_env_disables_bolding(fixture_db_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    # use_color=None => respect environment.
    with Searcher(db_path=fixture_db_path, use_color=None) as s:
        hits = s.search("mehrwerk", limit=1)
        assert hits
        assert ANSI_BOLD_OPEN not in hits[0].snippet


def test_use_color_explicit_overrides_env(fixture_db_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    with Searcher(db_path=fixture_db_path, use_color=True) as s:
        hits = s.search("mehrwerk", limit=1)
        assert hits
        # Explicit constructor arg wins over env.
        assert ANSI_BOLD_OPEN in hits[0].snippet


def test_snippet_window_around_first_match(fixture_db_path):
    """Snippet should center around the first match, not always start at 0."""
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        hits = s.search("dashboard", limit=3)
        # At least one drawer ('What about recall and search behaviour for the
        # Mehrwerk dashboard?') has 'dashboard' near the end. Its snippet
        # should still surface it.
        for h in hits:
            stripped = _strip_ansi(h.snippet)
            assert "dashboard" in stripped.lower()


def test_filter_by_source_passed_through(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        hits = s.search("lorem", source="markdown")
        assert hits
        assert all(h.source == "markdown" for h in hits)


def test_extract_match_terms_basic():
    assert _extract_match_terms("mehrwerk pricing") == ["mehrwerk", "pricing"]


def test_extract_match_terms_strips_fts5_operators():
    terms = _extract_match_terms("mehrwerk OR andrew NEAR/5 saaga")
    assert "mehrwerk" in terms
    assert "andrew" in terms
    assert "saaga" in terms
    assert "OR" not in terms
    assert "NEAR" not in terms


def test_extract_match_terms_handles_quotes():
    terms = _extract_match_terms('"mehrwerk pricing"')
    assert terms == ["mehrwerk", "pricing"]


def test_extract_match_terms_empty():
    assert _extract_match_terms("") == []


def test_first_match_index_finds_earliest():
    text = "no match here. then andrew. then mehrwerk."
    # andrew first, mehrwerk second
    assert _first_match_index(text, ["mehrwerk", "andrew"]) == text.lower().find("andrew")


def test_first_match_index_no_match():
    assert _first_match_index("hello world", ["zzzzz"]) is None


def test_empty_query_returns_no_hits(fixture_db_path):
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        assert s.search("") == []
        assert s.search("   ") == []


def test_searcher_close_idempotent(fixture_db_path):
    s = Searcher(db_path=fixture_db_path, use_color=False)
    s.close()
    # Second close should not raise.
    s.close()
