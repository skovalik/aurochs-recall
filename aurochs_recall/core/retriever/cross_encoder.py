"""Cross-encoder reranker — rescores BM25 candidates with a transformer.

Plan v5 reference: cross-encoder is the second-stage rerank over the FTS5
BM25 first stage. The pipeline:

    query  ─┐
            ├─►  FTS5Retriever.search()       → top-K' (over-fetched)
            │
            ├─►  CrossEncoderReranker.rerank() → top-K (rescored)
            │
    [drawer corpus]

Model selection (locked in plan v5 decisions table):

    cross-encoder/ms-marco-MiniLM-L-6-v2          (English, default)
    cross-encoder/mmarco-mMiniLMv2-L12-H384-v1    (multilingual, [multilingual] extra)

Optional dependency: ``sentence_transformers + torch`` (the ``[embeddings]``
extra in pyproject). If the dependency is missing, ``get_default_reranker()``
returns ``None`` so the Searcher can gracefully fall back to FTS5-only
ranking. Lazy imports keep cold-start fast for users who never invoke a
reranked search.

Threading: the underlying ``CrossEncoder.predict`` is not thread-safe
across calls but is safe within a single ``rerank()`` call. The reranker
is intended to be constructed once per process and reused; tests cover
the construct-fresh-each-call path too.

Memory: the MiniLM-L-6 model is ~22MB on disk and ~90MB in RAM with
torch CPU. The model is loaded lazily on first ``rerank()`` call to
amortize the load cost across the rest of the search pipeline.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from aurochs_recall.core.types import Drawer

if TYPE_CHECKING:  # pragma: no cover — import-time-only typing
    from sentence_transformers import CrossEncoder


# Locked in plan v5 (decisions table). Keep these literals here so a
# version bump is one diff in this file plus a bench/README update.
DEFAULT_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
MULTILINGUAL_MODEL: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"

# Maximum number of candidates we'll feed the cross-encoder per call.
# Reranking is O(K * model_inference_time) so over-fetching from FTS5 to
# 200+ candidates in a hot loop is a footgun. Caps the cost-per-query.
MAX_CANDIDATES: int = 200


class CrossEncoderUnavailableError(RuntimeError):
    """Raised when sentence-transformers is not installed and the caller
    asked for a reranker explicitly. ``get_default_reranker()`` swallows
    this and returns None instead so the Searcher can degrade gracefully.
    """


class CrossEncoderReranker:
    """Wraps a sentence-transformers CrossEncoder with the recall API shape.

    Construction is lightweight; the actual model is loaded lazily on the
    first ``rerank()`` call. This keeps `import aurochs_recall` fast for
    users who never run a reranked query.

    Parameters
    ----------
    model_name:
        HuggingFace model identifier. Defaults to the locked English MS-
        MARCO MiniLM. For multilingual content pass ``MULTILINGUAL_MODEL``
        (requires the same dependency, just a different download).
    device:
        Optional torch device string (``"cpu"``, ``"cuda"``, ``"mps"``).
        Default None lets sentence-transformers pick automatically.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        device: str | None = None,
    ) -> None:
        if not model_name:
            raise ValueError("model_name must be non-empty")
        self.model_name: str = model_name
        self.device: str | None = device
        self._model: CrossEncoder | None = None

    def _load_model(self) -> CrossEncoder:
        """Lazily import + instantiate the CrossEncoder."""
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover — exercised in test_cross_encoder
            raise CrossEncoderUnavailableError(
                "sentence-transformers is not installed. "
                "Install with: pip install aurochs-recall[embeddings]"
            ) from exc
        self._model = CrossEncoder(self.model_name, device=self.device)
        return self._model

    def rerank(
        self,
        query: str,
        candidates: list[Drawer],
        *,
        top_k: int = 10,
    ) -> list[tuple[Drawer, float]]:
        """Rescore ``candidates`` against ``query`` with the cross-encoder.

        The cross-encoder produces a single relevance logit per (query,
        candidate) pair — these are NOT calibrated probabilities, but they
        are sortable per query so we can pick the top ``top_k``.

        Empty inputs short-circuit: empty query or empty candidates returns
        an empty list. Candidates beyond ``MAX_CANDIDATES`` are dropped
        (FTS5 already ordered them by BM25 so the tail is the least
        promising bucket anyway).

        Returns
        -------
        list of (drawer, score) tuples in descending score order, length
        ``min(top_k, len(candidates), MAX_CANDIDATES)``.
        """
        if not query or not query.strip():
            return []
        if not candidates:
            return []
        if top_k <= 0:
            return []

        # Cap the input size so a runaway over-fetch can't blow up.
        capped = candidates[:MAX_CANDIDATES]
        model = self._load_model()

        # CrossEncoder.predict accepts list[tuple[str, str]]. Each pair is
        # (query, document). The model emits one score per pair.
        pairs = [(query, drawer.content) for drawer in capped]
        raw_scores = model.predict(pairs)
        # sentence-transformers returns numpy arrays for batched input. We
        # don't import numpy directly (would force the dep on light installs);
        # rely on the iterable + float() to coerce element-wise.
        scored: list[tuple[Drawer, float]] = [
            (drawer, float(score))
            for drawer, score in zip(capped, raw_scores, strict=True)
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]


def get_default_reranker(
    *,
    model_name: str | None = None,
    device: str | None = None,
) -> CrossEncoderReranker | None:
    """Return a ready-to-use reranker, or None if the optional dep is missing.

    Resolution order for ``model_name``:
      1. Explicit argument (caller wins).
      2. ``RECALL_RERANK_MODEL`` environment variable (operator override).
      3. ``DEFAULT_MODEL`` constant (locked in plan v5).

    The Searcher calls this on every search; on the first call it probes
    for sentence-transformers and either constructs a reranker or returns
    None. Subsequent calls re-probe but the result is cached at the import
    level by Python's module cache so the cost is one boolean check.

    Returns
    -------
    A ``CrossEncoderReranker`` if ``sentence_transformers`` can be imported,
    else None. The Searcher checks for None and skips the rerank stage.
    """
    name = model_name or os.environ.get("RECALL_RERANK_MODEL") or DEFAULT_MODEL
    try:
        # Light probe — just verify the import resolves. We don't actually
        # need the symbol; the reranker's _load_model() will re-import.
        import sentence_transformers  # noqa: F401
    except ImportError:
        return None
    return CrossEncoderReranker(model_name=name, device=device)


__all__ = [
    "DEFAULT_MODEL",
    "MAX_CANDIDATES",
    "MULTILINGUAL_MODEL",
    "CrossEncoderReranker",
    "CrossEncoderUnavailableError",
    "get_default_reranker",
]
