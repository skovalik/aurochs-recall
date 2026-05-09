"""FTS5 BM25 retriever — the only retriever in T0.

Joins drawers_fts (FTS5 virtual) ↔ drawer_meta on rowid. Returns hits ranked
by sqlite's built-in bm25() score (lower-magnitude-after-negation = more
relevant). Per the locked Hit contract the search method returns flat Hit
objects (drawer_uid + score + snippet + source + created_at + rank); full
drawer content is fetched separately via ``fetch_drawer`` when needed.

Query modes:
  literal (default) — quote-escape the entire query string. Safe for arbitrary
                      user input including FTS5 metacharacters.
  raw     (opt-in)  — query passed through verbatim. User responsible for
                      valid FTS5 syntax. Surfaced via ``--raw`` CLI flag and
                      the ``raw=True`` filter kwarg.

Filter kwargs honored:
  source (str | list[str]), since (int), until (int), register (str),
  role (str), limit (int), raw (bool)

Hidden drawers: if a ``hidden_drawers`` table exists, the join filters them
out. T0 schema may not include the table; absence is handled gracefully.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from aurochs_recall.core.types import Drawer, Hit


# Conservative caps so a runaway query can't OOM the process.
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


class FTS5QueryError(ValueError):
    """Raised when sqlite rejects the FTS5 MATCH expression.

    Most common when ``--raw`` is set and the user's expression has a syntax
    error. The CLI catches this and prints a readable hint.
    """

    def __init__(self, msg: str, *, query: str, raw: bool) -> None:
        super().__init__(msg)
        self.query = query
        self.raw = raw


class FTS5Retriever:
    """BM25 search over the drawers_fts virtual table.

    Constructed with an open ``sqlite3.Connection`` (preferred) or a
    ``db_path`` the retriever opens itself. The connection is expected to
    already have ``PRAGMA foreign_keys = ON`` and the standard pragmas
    applied — the spine's ``open_db()`` does this. We don't reapply pragmas
    so we don't fight over connection state.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        if conn is None and db_path is None:
            raise ValueError("FTS5Retriever requires either conn or db_path")
        self._owned_conn = conn is None
        if conn is None:
            self._conn = sqlite3.connect(str(db_path))
            self._conn.execute("PRAGMA foreign_keys = ON")
        else:
            self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._has_hidden_table = self._detect_table("hidden_drawers")

    def _detect_table(self, name: str) -> bool:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None

    def close(self) -> None:
        if self._owned_conn:
            self._conn.close()

    def __enter__(self) -> FTS5Retriever:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Query construction
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_literal(query: str) -> str:
        """Quote-escape a raw user string for safe FTS5 MATCH.

        FTS5 phrase syntax: ``"quoted string"``. Internal double-quotes are
        escaped by doubling. Returns the entire query as a single phrase.
        """
        return '"' + query.replace('"', '""') + '"'

    def _build_match_query(self, query: str, raw: bool) -> str:
        return query if raw else self._escape_literal(query)

    @staticmethod
    def _normalize_source_filter(source: Any) -> list[str] | None:
        if source is None:
            return None
        if isinstance(source, str):
            return [source]
        if isinstance(source, Iterable):
            vals = [str(s) for s in source if s]
            return vals or None
        return [str(source)]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, **filters: Any) -> list[Hit]:
        """Run a BM25 search; return flat Hit objects (no Drawer attached).

        Snippet field is left empty — the Searcher fills it in. Callers that
        need full drawer content for a Hit can use ``fetch_drawer(uid)``.
        """
        if not query or not query.strip():
            return []

        raw = bool(filters.get("raw", False))
        match_expr = self._build_match_query(query.strip(), raw)

        sources = self._normalize_source_filter(filters.get("source"))
        since = filters.get("since")
        until = filters.get("until")
        register = filters.get("register")
        role = filters.get("role")
        limit_in = filters.get("limit")

        try:
            limit = int(limit_in) if limit_in is not None else DEFAULT_LIMIT
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_LIMIT))

        sql_parts: list[str] = [
            "SELECT",
            "  m.drawer_uid, m.source, m.source_id, m.source_path, m.role,",
            "  m.register, m.thread_id, m.parent_uid, m.position_in_thread,",
            "  m.branch_count, m.created_at, m.content_hash,",
            "  m.risk_score, m.risk_score_version, m.hash_input_version,",
            "  f.content AS content,",
            "  bm25(drawers_fts) AS bm25_raw",
            "FROM drawers_fts AS f",
            "JOIN drawer_meta AS m ON m.rowid = f.rowid",
        ]
        if self._has_hidden_table:
            sql_parts.append(
                "LEFT JOIN hidden_drawers AS h ON h.drawer_uid = m.drawer_uid"
            )

        where: list[str] = ["drawers_fts MATCH ?"]
        params: list[Any] = [match_expr]

        if self._has_hidden_table:
            where.append("(h.drawer_uid IS NULL OR h.unhidden_at IS NOT NULL)")
        if sources:
            placeholders = ",".join("?" * len(sources))
            where.append(f"m.source IN ({placeholders})")
            params.extend(sources)
        if since is not None:
            where.append("m.created_at >= ?")
            params.append(int(since))
        if until is not None:
            where.append("m.created_at <= ?")
            params.append(int(until))
        if register is not None:
            where.append("m.register = ?")
            params.append(register)
        if role is not None:
            where.append("m.role = ?")
            params.append(role)

        sql_parts.append("WHERE " + " AND ".join(where))
        sql_parts.append("ORDER BY bm25_raw ASC")  # FTS5 bm25: lower = better
        sql_parts.append("LIMIT ?")
        params.append(limit)

        sql = "\n".join(sql_parts)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            raise FTS5QueryError(str(e), query=query, raw=raw) from e

        hits: list[Hit] = []
        for rank, row in enumerate(rows, start=1):
            # FTS5 bm25() returns negative scores by convention (lower=better).
            # Negate so "higher score = more relevant" matches Hit's spec.
            score = -float(row["bm25_raw"])
            hits.append(
                Hit(
                    drawer_uid=row["drawer_uid"],
                    score=score,
                    snippet="",  # Searcher fills with ANSI-bolded excerpt
                    source=row["source"],
                    created_at=int(row["created_at"]),
                    rank=rank,
                )
            )
        return hits

    # ------------------------------------------------------------------
    # Drawer fetch (used by Searcher for snippet generation, by CLI for
    # --full / --json / --open)
    # ------------------------------------------------------------------

    def fetch_drawer(self, drawer_uid: str) -> Drawer | None:
        """Load a full Drawer record by uid. Returns None if not found."""
        row = self._conn.execute(
            "SELECT m.source, m.source_id, m.source_path, m.role, m.register, "
            "       m.thread_id, m.parent_uid, m.position_in_thread, "
            "       m.branch_count, m.created_at, m.content_hash, "
            "       m.risk_score, m.risk_score_version, m.hash_input_version, "
            "       f.content AS content "
            "FROM drawer_meta AS m "
            "JOIN drawers_fts AS f ON f.rowid = m.rowid "
            "WHERE m.drawer_uid = ? "
            "LIMIT 1",
            (drawer_uid,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_drawer(row)

    def search_with_drawers(
        self, query: str, **filters: Any
    ) -> list[tuple[Hit, Drawer]]:
        """Same as search() but bundles each Hit with the source Drawer.

        Used by Searcher to render snippets without an extra round-trip per
        result. Public so CLI can hit it directly when --full or --json
        require content access.
        """
        if not query or not query.strip():
            return []

        raw = bool(filters.get("raw", False))
        match_expr = self._build_match_query(query.strip(), raw)

        sources = self._normalize_source_filter(filters.get("source"))
        since = filters.get("since")
        until = filters.get("until")
        register = filters.get("register")
        role = filters.get("role")
        limit_in = filters.get("limit")

        try:
            limit = int(limit_in) if limit_in is not None else DEFAULT_LIMIT
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, MAX_LIMIT))

        sql_parts: list[str] = [
            "SELECT",
            "  m.drawer_uid, m.source, m.source_id, m.source_path, m.role,",
            "  m.register, m.thread_id, m.parent_uid, m.position_in_thread,",
            "  m.branch_count, m.created_at, m.content_hash,",
            "  m.risk_score, m.risk_score_version, m.hash_input_version,",
            "  f.content AS content,",
            "  bm25(drawers_fts) AS bm25_raw",
            "FROM drawers_fts AS f",
            "JOIN drawer_meta AS m ON m.rowid = f.rowid",
        ]
        if self._has_hidden_table:
            sql_parts.append(
                "LEFT JOIN hidden_drawers AS h ON h.drawer_uid = m.drawer_uid"
            )

        where: list[str] = ["drawers_fts MATCH ?"]
        params: list[Any] = [match_expr]
        if self._has_hidden_table:
            where.append("(h.drawer_uid IS NULL OR h.unhidden_at IS NOT NULL)")
        if sources:
            placeholders = ",".join("?" * len(sources))
            where.append(f"m.source IN ({placeholders})")
            params.extend(sources)
        if since is not None:
            where.append("m.created_at >= ?")
            params.append(int(since))
        if until is not None:
            where.append("m.created_at <= ?")
            params.append(int(until))
        if register is not None:
            where.append("m.register = ?")
            params.append(register)
        if role is not None:
            where.append("m.role = ?")
            params.append(role)

        sql_parts.append("WHERE " + " AND ".join(where))
        sql_parts.append("ORDER BY bm25_raw ASC")
        sql_parts.append("LIMIT ?")
        params.append(limit)

        sql = "\n".join(sql_parts)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            raise FTS5QueryError(str(e), query=query, raw=raw) from e

        out: list[tuple[Hit, Drawer]] = []
        for rank, row in enumerate(rows, start=1):
            score = -float(row["bm25_raw"])
            drawer = _row_to_drawer(row)
            hit = Hit(
                drawer_uid=row["drawer_uid"],
                score=score,
                snippet="",
                source=row["source"],
                created_at=int(row["created_at"]),
                rank=rank,
            )
            out.append((hit, drawer))
        return out


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_to_drawer(row: sqlite3.Row) -> Drawer:
    return Drawer(
        source=row["source"],
        source_id=row["source_id"],
        role=row["role"],
        content=row["content"] or "",
        created_at=int(row["created_at"]),
        content_hash=row["content_hash"],
        source_path=row["source_path"],
        register=row["register"],
        thread_id=row["thread_id"],
        parent_uid=row["parent_uid"],
        position_in_thread=row["position_in_thread"],
        branch_count=int(row["branch_count"] or 0),
        risk_score=int(row["risk_score"] or 0),
        risk_score_version=int(row["risk_score_version"] or 1),
        hash_input_version=int(row["hash_input_version"] or 1),
    )
