"""Cross-encoder reranker tests — graceful degrade + minimal-API contract.

These tests run cleanly in two scenarios:

  1. ``[embeddings]`` extra NOT installed (default CI / local dev) —
     verifies that ``get_default_reranker()`` returns None and that
     constructing a reranker explicitly + calling ``rerank()`` raises
     ``CrossEncoderUnavailableError``.

  2. ``[embeddings]`` extra IS installed — the heavy model-load test
     uses ``pytest.importorskip`` and skips automatically when
     sentence-transformers is not present.

The model-load path also gates on ``RECALL_TEST_DOWNLOAD_MODELS=1`` so
local devs without the model cached aren't surprised by a 22MB download
when running ``pytest``. CI can opt in by setting the envvar.
"""
from __future__ import annotations

import os

import pytest

from aurochs_recall.core.retriever.cross_encoder import (
    DEFAULT_MODEL,
    MAX_CANDIDATES,
    MULTILINGUAL_MODEL,
    CrossEncoderReranker,
    CrossEncoderUnavailableError,
    get_default_reranker,
)
from aurochs_recall.core.types import Drawer


def _make_drawer(content: str, *, source_id: str = "1") -> Drawer:
    return Drawer(
        source="test",
        source_id=source_id,
        role="human",
        content=content,
        created_at=0,
    )


# ---------------------------------------------------------------------------
# Constants + factory probes (run with or without the extra)
# ---------------------------------------------------------------------------


def test_default_model_is_locked() -> None:
    """Plan v5 locks the model name; this test asserts the constant
    actually points at the locked value (catches accidental edits)."""
    assert DEFAULT_MODEL == "cross-encoder/ms-marco-MiniLM-L-6-v2"


def test_multilingual_model_is_locked() -> None:
    assert MULTILINGUAL_MODEL == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


def test_max_candidates_is_bounded() -> None:
    """The cap exists to keep rerank cost finite. Sanity-check the value."""
    assert 50 <= MAX_CANDIDATES <= 1000


def test_reranker_construction_does_not_load_model() -> None:
    """Construction is lightweight — model loads on first rerank call."""
    r = CrossEncoderReranker(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2")
    assert r.model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert r._model is None  # not yet loaded


def test_reranker_rejects_empty_model_name() -> None:
    with pytest.raises(ValueError, match="model_name must be non-empty"):
        CrossEncoderReranker(model_name="")


# ---------------------------------------------------------------------------
# Graceful degrade when [embeddings] is absent
# ---------------------------------------------------------------------------


def _embeddings_extra_installed() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    _embeddings_extra_installed(),
    reason="[embeddings] extra IS installed; this test covers the missing-extra path",
)
def test_get_default_reranker_returns_none_without_extra() -> None:
    """When sentence-transformers is missing, the factory returns None
    so the Searcher can degrade gracefully to BM25-only."""
    assert get_default_reranker() is None


@pytest.mark.skipif(
    _embeddings_extra_installed(),
    reason="[embeddings] extra IS installed; this test covers the missing-extra path",
)
def test_rerank_raises_unavailable_when_extra_missing() -> None:
    """Explicit construction + rerank should raise when the dep is absent."""
    r = CrossEncoderReranker()
    drawer = _make_drawer("hello world")
    with pytest.raises(CrossEncoderUnavailableError):
        r.rerank("hello", [drawer])


# ---------------------------------------------------------------------------
# Empty / edge inputs (work in both scenarios — short-circuit before load)
# ---------------------------------------------------------------------------


def test_rerank_empty_query_returns_empty() -> None:
    """Empty query short-circuits before model load — works without the extra."""
    r = CrossEncoderReranker()
    drawer = _make_drawer("hello world")
    assert r.rerank("", [drawer]) == []
    assert r.rerank("   ", [drawer]) == []


def test_rerank_empty_candidates_returns_empty() -> None:
    """Empty candidates short-circuits before model load."""
    r = CrossEncoderReranker()
    assert r.rerank("query", []) == []


def test_rerank_zero_top_k_returns_empty() -> None:
    """top_k <= 0 short-circuits before model load."""
    r = CrossEncoderReranker()
    drawer = _make_drawer("hello")
    assert r.rerank("query", [drawer], top_k=0) == []


# ---------------------------------------------------------------------------
# Real model load (only when [embeddings] AND opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_rerank_orders_relevant_higher() -> None:
    """Smoke test against the real model.

    Skipped automatically when sentence-transformers is missing or when
    ``RECALL_TEST_DOWNLOAD_MODELS=1`` is not set. Set the envvar in CI
    to opt into the model download + inference cost.
    """
    pytest.importorskip("sentence_transformers")
    if os.environ.get("RECALL_TEST_DOWNLOAD_MODELS") != "1":
        pytest.skip("Set RECALL_TEST_DOWNLOAD_MODELS=1 to run model-load tests")

    r = CrossEncoderReranker()
    candidates = [
        _make_drawer("Cross-encoders rerank top-K candidates with much higher precision than BM25.", source_id="1"),
        _make_drawer("My favorite cookie recipe uses almond flour and dark chocolate.", source_id="2"),
        _make_drawer("FTS5 is sqlite's full-text search extension over BM25.", source_id="3"),
    ]
    scored = r.rerank("how do cross encoders improve search ranking", candidates, top_k=3)
    assert len(scored) == 3
    # The first drawer is most directly on-topic; assert it's not last.
    drawer_ids = [d.source_id for d, _ in scored]
    assert drawer_ids[0] == "1"


@pytest.mark.slow
def test_real_rerank_caps_at_max_candidates() -> None:
    """Rerank should drop candidates beyond MAX_CANDIDATES."""
    pytest.importorskip("sentence_transformers")
    if os.environ.get("RECALL_TEST_DOWNLOAD_MODELS") != "1":
        pytest.skip("Set RECALL_TEST_DOWNLOAD_MODELS=1 to run model-load tests")

    r = CrossEncoderReranker()
    drawers = [_make_drawer(f"document {i}", source_id=str(i)) for i in range(MAX_CANDIDATES + 50)]
    scored = r.rerank("query", drawers, top_k=10)
    # Only the first MAX_CANDIDATES were considered, so all returned IDs
    # should be < MAX_CANDIDATES.
    for d, _ in scored:
        assert int(d.source_id) < MAX_CANDIDATES
