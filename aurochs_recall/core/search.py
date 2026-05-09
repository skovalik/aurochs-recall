"""Searcher — wraps a Retriever with snippet generation + ANSI bold.

The Searcher is the boundary the CLI and (later) MCP server call into. It
owns: mode dispatch (T0: bm25 only), snippet windowing, ANSI bold rendering
with NO_COLOR / non-tty degradation. The Retriever is pluggable so we can
introduce HybridRetriever / CrossEncoderRetriever in later patches without
touching call sites.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Literal

from aurochs_recall.core.retriever.fts5 import FTS5Retriever
from aurochs_recall.core.types import Drawer, Hit


# ANSI bold around matched terms. Unicode-safe (no fixed-byte assumptions).
ANSI_BOLD_OPEN = "\x1b[1m"
ANSI_BOLD_CLOSE = "\x1b[22m"

# Snippet window: roughly 2 lines of terminal output ≈ 200 chars total.
SNIPPET_RADIUS = 80
SNIPPET_MAX_LEN = 200


SearchMode = Literal["bm25"]  # T0 only; "hybrid" / "semantic" added later


class Searcher:
    """High-level search facade for the CLI and MCP server.

    Default mode is `bm25` (the only mode in T0). Snippet generation runs in
    Python after the retriever returns hits — keeps SQL clean and lets us
    swap retrievers without touching snippet logic.

    The ``last_drawers`` attribute is populated after each ``search()`` call
    with the Drawer objects backing each Hit (in the same order). The CLI
    uses this for ``--full`` and ``--json`` output without re-querying.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection | None = None,
        db_path: Path | str | None = None,
        retriever: FTS5Retriever | None = None,
        use_color: bool | None = None,
    ) -> None:
        if retriever is None:
            retriever = FTS5Retriever(conn=conn, db_path=db_path)
            self._owned_retriever = True
        else:
            self._owned_retriever = False
        self._retriever = retriever

        # use_color resolution order:
        #   1. explicit constructor arg (caller wins)
        #   2. NO_COLOR env var (any non-empty value disables color)
        #   3. stdout isatty
        if use_color is None:
            if os.environ.get("NO_COLOR"):
                use_color = False
            else:
                use_color = sys.stdout.isatty()
        self._use_color = use_color

        # Filled after each search() call so callers can fetch the backing
        # drawer content without re-querying.
        self.last_drawers: list[Drawer] = []

    def close(self) -> None:
        if self._owned_retriever and hasattr(self._retriever, "close"):
            self._retriever.close()

    def __enter__(self) -> Searcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public search entry points
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        mode: SearchMode = "bm25",
        full: bool = False,
        **filters: object,
    ) -> list[Hit]:
        """Run a search and decorate each Hit with a snippet.

        Args:
            query: user query string.
            mode: search mode. T0 only supports "bm25".
            full: if True, snippet field carries full content (no truncation).
            **filters: passed through to the retriever (source, since, until,
                       register, role, limit, raw).

        After return, ``self.last_drawers`` holds the Drawer objects backing
        each Hit (in matching order).
        """
        if mode != "bm25":
            raise ValueError(
                f"Unsupported search mode: {mode!r}. T0 only supports 'bm25'."
            )

        pairs = self._retriever.search_with_drawers(query, **filters)
        terms = _extract_match_terms(query)

        decorated_hits: list[Hit] = []
        drawers: list[Drawer] = []
        for hit, drawer in pairs:
            if full:
                snippet = self._format_full(drawer.content, terms)
            else:
                snippet = self._format_snippet(drawer.content, terms)
            decorated_hits.append(replace(hit, snippet=snippet))
            drawers.append(drawer)
        self.last_drawers = drawers
        return decorated_hits

    # ------------------------------------------------------------------
    # Snippet formatting
    # ------------------------------------------------------------------

    def _format_snippet(self, content: str, terms: list[str]) -> str:
        """Window around the first match; bold matched terms; cap length."""
        if not content:
            return ""
        flat = content.replace("\n", " ").replace("\r", " ")
        flat = re.sub(r"\s+", " ", flat).strip()
        if not flat:
            return ""

        first_match = _first_match_index(flat, terms)
        if first_match is None:
            window = flat[:SNIPPET_MAX_LEN]
            prefix = ""
            suffix = "..." if len(flat) > SNIPPET_MAX_LEN else ""
        else:
            start = max(0, first_match - SNIPPET_RADIUS)
            end = min(len(flat), first_match + SNIPPET_RADIUS + 40)
            if end - start > SNIPPET_MAX_LEN:
                end = start + SNIPPET_MAX_LEN
            window = flat[start:end]
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(flat) else ""

        bolded = self._bold_terms(window, terms)
        return f"{prefix}{bolded}{suffix}"

    def _format_full(self, content: str, terms: list[str]) -> str:
        if not content:
            return ""
        return self._bold_terms(content, terms)

    def _bold_terms(self, text: str, terms: list[str]) -> str:
        if not terms or not self._use_color:
            return text
        # Build a single regex that matches any of the terms, longest-first
        # to avoid prefix-shadowing (e.g. "pric" matching before "pricing").
        sorted_terms = sorted({t for t in terms if t}, key=len, reverse=True)
        if not sorted_terms:
            return text
        pattern = re.compile(
            r"(" + "|".join(re.escape(t) for t in sorted_terms) + r")",
            re.IGNORECASE,
        )
        return pattern.sub(
            lambda m: f"{ANSI_BOLD_OPEN}{m.group(0)}{ANSI_BOLD_CLOSE}", text
        )


# ----------------------------------------------------------------------
# Term extraction
# ----------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[\w][\w\-']*", flags=re.UNICODE)


def _extract_match_terms(query: str) -> list[str]:
    """Pull bare tokens out of a (possibly FTS5-syntax) query for snippet bolding.

    For literal queries this is just the user's words. For raw queries we
    strip FTS5 operators (NEAR, OR, NOT, parentheses, prefix*, column:) and
    keep the remaining tokens. Best-effort — bolding mismatches don't break
    correctness.
    """
    if not query:
        return []
    cleaned = re.sub(
        r"\b(NEAR|AND|OR|NOT)\b|[(){}\[\]\"\*\^:]",
        " ",
        query,
    )
    return _TOKEN_RE.findall(cleaned)


def _first_match_index(text: str, terms: list[str]) -> int | None:
    if not terms:
        return None
    lowered = text.lower()
    earliest: int | None = None
    for term in terms:
        idx = lowered.find(term.lower())
        if idx != -1 and (earliest is None or idx < earliest):
            earliest = idx
    return earliest
