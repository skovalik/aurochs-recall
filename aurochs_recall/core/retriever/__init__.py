"""Retriever layer — pluggable search strategies over the drawer index.

T0 ships only the BM25 (FTS5) retriever. Hybrid + cross-encoder + LLM-rerank
retrievers slot in behind the same Protocol in later patches.
"""
from __future__ import annotations

from aurochs_recall.core.retriever._base import Retriever
from aurochs_recall.core.retriever.fts5 import FTS5QueryError, FTS5Retriever

__all__ = ["Retriever", "FTS5Retriever", "FTS5QueryError"]
