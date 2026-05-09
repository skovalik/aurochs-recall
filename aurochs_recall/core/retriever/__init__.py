"""Retriever layer — pluggable search strategies over the drawer index.

T0 ships the BM25 (FTS5) retriever. T1 adds the cross-encoder reranker
that the Searcher composes on top of FTS5 candidates. Hybrid + LLM-rerank
retrievers slot in behind the same Protocol in later patches.
"""
from __future__ import annotations

from aurochs_recall.core.retriever._base import Retriever
from aurochs_recall.core.retriever.cross_encoder import (
    DEFAULT_MODEL,
    MAX_CANDIDATES,
    MULTILINGUAL_MODEL,
    CrossEncoderReranker,
    CrossEncoderUnavailableError,
    get_default_reranker,
)
from aurochs_recall.core.retriever.fts5 import FTS5QueryError, FTS5Retriever

__all__ = [
    "DEFAULT_MODEL",
    "MAX_CANDIDATES",
    "MULTILINGUAL_MODEL",
    "CrossEncoderReranker",
    "CrossEncoderUnavailableError",
    "FTS5QueryError",
    "FTS5Retriever",
    "Retriever",
    "get_default_reranker",
]
