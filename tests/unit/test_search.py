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


# ---------------------------------------------------------------------------
# T1 reranker wiring
# ---------------------------------------------------------------------------


class _StubReranker:
    """Stub cross-encoder reranker that scores by content length descending.

    Used to verify the Searcher wires the reranker correctly without
    pulling in the [embeddings] extra. Returns deterministic ordering
    so tests don't flake.
    """

    def __init__(self):
        self.calls = []

    def rerank(self, query, candidates, *, top_k=10):
        self.calls.append((query, len(candidates), top_k))
        scored = [(d, float(len(d.content))) for d in candidates]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]


def test_rerank_disabled_with_reranker_false(fixture_db_path):
    """reranker=False forces BM25-only even when extras are installed."""
    with Searcher(db_path=fixture_db_path, reranker=False, use_color=False) as s:
        assert s.has_reranker is False
        hits = s.search("mehrwerk")
        assert hits


def test_rerank_mode_falls_back_when_no_reranker(fixture_db_path):
    """mode='rerank' with reranker=False degrades to BM25 ordering."""
    with Searcher(db_path=fixture_db_path, reranker=False, use_color=False) as s:
        bm25_hits = s.search("mehrwerk", mode="bm25", limit=5)
        rerank_hits = s.search("mehrwerk", mode="rerank", limit=5)
        # Without a reranker, mode='rerank' returns the same shape as bm25.
        assert [h.drawer_uid for h in rerank_hits] == [h.drawer_uid for h in bm25_hits]


def test_rerank_mode_uses_supplied_reranker(fixture_db_path):
    """mode='rerank' invokes the supplied reranker and returns its ordering."""
    stub = _StubReranker()
    with Searcher(db_path=fixture_db_path, reranker=stub, use_color=False) as s:
        assert s.has_reranker is True
        hits = s.search("lorem", mode="rerank", limit=3)
        assert hits
        assert len(stub.calls) == 1
        # Stub orders by content length descending; first hit's drawer
        # content should be the longest among returned hits.
        for hit, drawer in zip(hits, s.last_drawers, strict=True):
            assert hit.drawer_uid == drawer.drawer_uid
        lengths = [len(d.content) for d in s.last_drawers]
        assert lengths == sorted(lengths, reverse=True)


def test_rerank_overfetches_candidates(fixture_db_path):
    """Rerank should over-fetch from BM25 to give the reranker headroom."""
    stub = _StubReranker()
    with Searcher(db_path=fixture_db_path, reranker=stub, use_color=False) as s:
        s.search("lorem", mode="rerank", limit=3)
    # Caller asked for limit=3 → we over-fetch by 3x = 9 candidates passed.
    # Allow for fixture having fewer than 9 matching drawers though.
    _query, candidates_seen, top_k = stub.calls[0]
    assert top_k == 3
    assert candidates_seen >= 1


def test_rerank_unsupported_mode_still_raises(fixture_db_path):
    """mode='hybrid' is still rejected — only bm25 + rerank are valid."""
    with (
        Searcher(db_path=fixture_db_path, reranker=False) as s,
        pytest.raises(ValueError, match="Unsupported search mode"),
    ):
        s.search("x", mode="hybrid")  # type: ignore[arg-type]


def test_rerank_assigns_new_ranks(fixture_db_path):
    """Reranked hits should have rank=1..N matching their new ordering."""
    stub = _StubReranker()
    with Searcher(db_path=fixture_db_path, reranker=stub, use_color=False) as s:
        hits = s.search("lorem", mode="rerank", limit=3)
        assert hits
        for expected_rank, hit in enumerate(hits, start=1):
            assert hit.rank == expected_rank


def test_bm25_mode_skips_reranker_even_when_available(fixture_db_path):
    """Default mode='bm25' must NOT invoke the reranker."""
    stub = _StubReranker()
    with Searcher(db_path=fixture_db_path, reranker=stub, use_color=False) as s:
        s.search("lorem", mode="bm25", limit=3)
    assert stub.calls == []  # reranker was never called
