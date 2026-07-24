"""aurochs-recall — memory architecture for AI conversations.

T0 spine: schema, types, indexer, graph store, OS-level locks, recovery.
T1 polish: cross-encoder rerank, BYOK extraction, multi-pass risk_score.
Plan reference: 2026-05-07-aurochs-recall-plan-v5.md (and v4).

License: MIT — see LICENSE for full text and author contact.
"""

from __future__ import annotations

# Cross-encoder reranker (lazy graceful-degrade).
from aurochs_recall.core.retriever.cross_encoder import (
    CrossEncoderReranker,
    get_default_reranker,
)

# BYOK LLM extraction layer.
from aurochs_recall.core.extraction import (
    BYOKExtractionUnavailableError,
    ExtractionResult,
    ExtractionRunner,
    ExtractionStatus,
)

# Multi-pass risk_score scanner.
from aurochs_recall.core.validation import (
    RiskScore,
    classify_risk_band,
    compute_risk_score,
)

__version__ = "0.2.0"
__license__ = "MIT"

__all__ = [
    "BYOKExtractionUnavailableError",
    "CrossEncoderReranker",
    "ExtractionResult",
    "ExtractionRunner",
    "ExtractionStatus",
    "RiskScore",
    "__license__",
    "__version__",
    "classify_risk_band",
    "compute_risk_score",
    "get_default_reranker",
]
