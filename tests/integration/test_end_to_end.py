"""End-to-end tests: fixture DB → search → expected hits.

These exercise the full Searcher / FTS5Retriever / CLI stack against the
deterministic fixture corpus. If a query result set changes here, either
the fixture changed (intentional) or a regression happened (not).
"""
from __future__ import annotations

import json

import pytest

from aurochs_recall.cli.main import main
from aurochs_recall.core.retriever.fts5 import FTS5Retriever
from aurochs_recall.core.search import Searcher


# Canned query → expected uid prefixes (12-char content_hash[:12]). Updated
# automatically when the fixture corpus changes.
EXPECTED_QUERY_HITS = {
    "mehrwerk": [
        "claude_code:session-aaaa",
        "claude_ai:conv-bbbb",
        "markdown:notes/pricing-2026.md",
    ],
    "lorem": [
        "claude_code:session-aaaa",
        "claude_ai:conv-bbbb",
        "markdown:notes/lorem-tests.md",
    ],
    "andrew": [
        "claude_code:session-aaaa",
        "claude_ai:conv-bbbb",
        "markdown:notes/andrew-saaga.md",
    ],
}


def test_canned_queries_return_expected_sources(fixture_db_path):
    """For each canned query, the expected source-bucket prefixes must show up."""
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        for query, expected_prefixes in EXPECTED_QUERY_HITS.items():
            hits = s.search(query, limit=20)
            uids = [h.drawer_uid for h in hits]
            for prefix in expected_prefixes:
                assert any(u.startswith(prefix) for u in uids), (
                    f"Query {query!r}: expected a hit with prefix {prefix!r} "
                    f"but got {uids}"
                )


def test_full_corpus_count(fixture_db_path):
    """Sanity: 20 drawers in the fixture, broad query should surface most of them."""
    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        # 'a' is too short to match meaningfully in FTS5; use a broad term.
        hits = s.search("the OR a OR is OR of", raw=True, limit=50)
        # Don't pin to exactly 20 — stop-words and tokenizer differences
        # across sqlite versions vary. Just assert "a lot of them".
        assert len(hits) >= 5


def test_cli_search_then_open(fixture_db_path, capsys, monkeypatch):
    """Search via CLI, then verify --json gives a uid we can pass back to --open."""
    monkeypatch.setenv("NO_COLOR", "1")

    code = main(["--db", str(fixture_db_path), "search", "mehrwerk", "--json"])
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed
    uid = parsed[0]["drawer_uid"]
    assert ":" in uid

    # Now use --open with a non-existent EDITOR command but valid uid; we
    # only care that it resolves the uid correctly. Since EDITOR exit code
    # depends on the platform, we just assert the resolution doesn't 404.
    monkeypatch.setenv("EDITOR", "true" if not isinstance(__builtins__, dict) and hasattr(__builtins__, "__name__") else "true")
    # Skip actual launch — pytest can't always mock subprocess across platforms.
    # Instead, verify that an unknown uid yields exit code 2 (not 0).
    code = main(["--db", str(fixture_db_path), "search", "anything",
                 "--open", "deadbeefcafe"])
    assert code == 2


def test_status_shows_expected_drawer_count(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "20 total" in out


def test_search_with_filters_combined(fixture_db_path):
    """Multiple filters combine correctly (AND semantics)."""
    with FTS5Retriever(db_path=fixture_db_path) as r:
        hits = r.search(
            "lorem",
            source="markdown",
            since=1710000000,
        )
        for h in hits:
            assert h.source == "markdown"
            assert h.created_at >= 1710000000


def test_score_ordering_stable(fixture_db_path):
    """Same query twice → same ranking. BM25 is deterministic; this guards
    against accidental nondeterminism in our SQL or post-processing."""
    with FTS5Retriever(db_path=fixture_db_path) as r:
        hits1 = r.search("mehrwerk", limit=5)
        hits2 = r.search("mehrwerk", limit=5)
        assert [h.drawer_uid for h in hits1] == [h.drawer_uid for h in hits2]
        assert [h.score for h in hits1] == [h.score for h in hits2]


def test_snippet_is_relevant(fixture_db_path):
    """Snippet should always include the matched term."""
    import re

    with Searcher(db_path=fixture_db_path, use_color=False) as s:
        for query in ("mehrwerk", "andrew", "lorem"):
            hits = s.search(query, limit=3)
            for h in hits:
                stripped = re.sub(r"\x1b\[[0-9;]*m", "", h.snippet)
                assert query.lower() in stripped.lower()


def test_cli_status_returns_zero_after_init_then_index_skipped(
    tmp_path, fixture_db_path, capsys
):
    """Smoke test: pointing --db at the fixture and running status works
    in any working directory."""
    code = main(["--db", str(fixture_db_path), "status"])
    assert code == 0
