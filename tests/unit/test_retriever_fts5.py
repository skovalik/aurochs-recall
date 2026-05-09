"""FTS5Retriever unit tests — BM25 ranking, filters, literal vs raw mode."""
from __future__ import annotations

import pytest

from aurochs_recall.core.retriever.fts5 import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FTS5QueryError,
    FTS5Retriever,
)


def test_basic_query_returns_hits(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("mehrwerk")
    assert len(hits) >= 3
    assert all(h.rank >= 1 for h in hits)
    # Lower rank index = better — rank 1 first
    assert hits[0].rank == 1
    # All hits should have the search term in some descendant — score is
    # higher = better in our convention.
    assert all(h.score > 0 for h in hits)


def test_empty_query_returns_empty(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    assert r.search("") == []
    assert r.search("   ") == []


def test_no_match_returns_empty(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    assert r.search("zzzzzzz_no_such_word_qqqq") == []


def test_literal_mode_quotes_special_chars(fixture_conn):
    """A query with FTS5 metacharacters should not raise in literal mode."""
    r = FTS5Retriever(conn=fixture_conn)
    # Parens and quotes would break a raw FTS5 expression.
    hits = r.search('mehrwerk (test) "quote"')
    # Doesn't have to find anything, but must not raise.
    assert isinstance(hits, list)


def test_raw_mode_supports_or(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("mehrwerk OR andrew", raw=True)
    # Should find drawers matching either term — more than just "mehrwerk".
    only_mehrwerk = r.search("mehrwerk")
    assert len(hits) > len(only_mehrwerk)


def test_raw_mode_invalid_syntax_raises(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    with pytest.raises(FTS5QueryError):
        r.search("(((unbalanced", raw=True)


def test_filter_by_source_string(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", source="markdown")
    assert all(h.source == "markdown" for h in hits)


def test_filter_by_source_list(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", source=["claude_code", "claude_ai"])
    assert {h.source for h in hits} <= {"claude_code", "claude_ai"}


def test_filter_by_since(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", since=1705000000)
    assert all(h.created_at >= 1705000000 for h in hits)


def test_filter_by_until(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", until=1705000000)
    assert all(h.created_at <= 1705000000 for h in hits)


def test_filter_by_register(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", register="teaching")
    # Need to fetch the drawers to verify the register column.
    for h in hits:
        d = r.fetch_drawer(h.drawer_uid)
        assert d is not None and d.register == "teaching"


def test_filter_by_role(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("mehrwerk", role="assistant")
    for h in hits:
        d = r.fetch_drawer(h.drawer_uid)
        assert d is not None and d.role == "assistant"


def test_limit_caps_results(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", limit=2)
    assert len(hits) <= 2


def test_limit_is_clamped(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem", limit=99999)
    # Even an absurd limit shouldn't error; clamped to MAX_LIMIT.
    assert len(hits) <= MAX_LIMIT


def test_default_limit_applied_when_omitted(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("lorem")
    assert len(hits) <= DEFAULT_LIMIT


def test_fetch_drawer_round_trip(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("mehrwerk", limit=1)
    assert hits
    d = r.fetch_drawer(hits[0].drawer_uid)
    assert d is not None
    assert d.drawer_uid == hits[0].drawer_uid
    assert "mehrwerk" in d.content.lower()


def test_fetch_drawer_unknown_returns_none(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    assert r.fetch_drawer("does:not:exist123456") is None


def test_search_with_drawers_pairs(fixture_conn):
    r = FTS5Retriever(conn=fixture_conn)
    pairs = r.search_with_drawers("mehrwerk", limit=3)
    assert len(pairs) >= 1
    for hit, drawer in pairs:
        assert hit.drawer_uid == drawer.drawer_uid


def test_protocol_compatibility(fixture_conn):
    """FTS5Retriever satisfies the runtime-checkable Retriever Protocol."""
    from aurochs_recall.core.retriever import Retriever

    r = FTS5Retriever(conn=fixture_conn)
    assert isinstance(r, Retriever)


def test_db_path_constructor(fixture_db_path):
    """Constructor without conn opens its own."""
    r = FTS5Retriever(db_path=fixture_db_path)
    try:
        hits = r.search("mehrwerk")
        assert hits
    finally:
        r.close()


def test_constructor_requires_conn_or_path():
    with pytest.raises(ValueError, match="conn or db_path"):
        FTS5Retriever()


def test_bm25_ordering_best_first(fixture_conn):
    """Higher score = better; rank should align with score order."""
    r = FTS5Retriever(conn=fixture_conn)
    hits = r.search("mehrwerk", limit=10)
    if len(hits) > 1:
        for a, b in zip(hits, hits[1:]):
            assert a.score >= b.score
            assert a.rank < b.rank
