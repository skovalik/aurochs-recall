"""Retriever Protocol — the single seam between Searcher and storage.

A Retriever takes a query string + filter kwargs and returns a list of Hit
objects ranked best-first. The Protocol intentionally has no notion of mode or
reranking — those are Searcher concerns. Retrievers are pure: query in,
ranked hits out, no side effects on the database.

T0 binding: only FTS5Retriever exists. Hybrid retriever (FTS5 + dense + RRF)
and CrossEncoderRetriever (rerank wrapper) plug in here in later patches.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.types import Hit


@runtime_checkable
class Retriever(Protocol):
    """A search strategy that returns ranked Hits for a query.

    Implementations MUST:
      - Return hits ordered best-first (lower rank index = better).
      - Honor `limit` filter; default to a sane cap (e.g. 50) if absent.
      - Treat unknown filter kwargs as a no-op (forward-compat) rather than
        raising — so older retrievers don't break newer Searchers passing
        future filter keys.

    Filter kwargs (T0 set; FTS5Retriever consumes all of these):
      source: list[str] | str | None     — restrict to one or more sources
      since:  int | None                 — created_at >= since (epoch seconds)
      until:  int | None                 — created_at <= until (epoch seconds)
      register: str | None               — drawer_meta.register exact match
      role:   str | None                 — drawer_meta.role exact match
      limit:  int | None                 — max hits to return
      raw:    bool                       — if True, query is FTS5 syntax verbatim
    """

    def search(self, query: str, **filters: object) -> list[Hit]: ...
