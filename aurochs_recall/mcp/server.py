"""MCP server for aurochs-recall.

Exposes 5 tools per plan v5 spec:

  - recall_search        BM25 search over the index
  - recall_drawer        full drawer fetch by uid (or unique prefix)
  - recall_status        DB stats including wal_size_pages
  - recall_graph_query   knowledge-graph entity / relationship lookup
  - recall_forget        soft-delete by drawer_uid (full or unique prefix)

All tools call into ``aurochs_recall.core`` and ``aurochs_recall.cli``
helpers; the MCP layer is a thin shim. The CLI is the source of truth
(see CLI reference); MCP behavior follows CLI behavior by construction.

Run via stdio:

    python -m aurochs_recall.mcp.server

The server reads `RECALL_DB` from the environment if set; otherwise it
falls back to the discovery chain in ``core.sources_config``.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aurochs_recall.cli.main import (
    _HIDDEN_DRAWERS_DDL,
    _AmbiguousPrefixError,
    _DrawerNotFoundError,
    resolve_drawer_uid_prefix,
)
from aurochs_recall.core.sources_config import (
    default_database_path,
    load_sources_config,
)


def _resolve_db_path() -> Path:
    """Resolve recall.db path: $RECALL_DB > sources.toml > default."""
    env = os.environ.get("RECALL_DB")
    if env:
        return Path(env).expanduser().resolve()
    try:
        cfg = load_sources_config(None)
        return cfg.database_path
    except FileNotFoundError:
        return default_database_path()


def _open_conn() -> sqlite3.Connection:
    db_path = _resolve_db_path()
    if not db_path.exists():
        raise FileNotFoundError(
            f"recall.db not found at {db_path}. "
            "Run `recall init` then `recall index` first."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ----------------------------------------------------------------------
# Tool implementations (pure-Python; reusable from tests)
# ----------------------------------------------------------------------


def _do_recall_search(
    query: str,
    *,
    top_k: int = 10,
    source: list[str] | None = None,
    register: str | None = None,
    role: str | None = None,
    since: int | None = None,
    until: int | None = None,
) -> dict[str, Any]:
    """Implementation backing the recall_search MCP tool.

    Returns a JSON-serializable dict with the hit list. The hit shape
    matches `recall search --json` exactly so MCP and CLI consumers
    speak the same language.
    """
    from aurochs_recall.core.retriever.fts5 import FTS5QueryError
    from aurochs_recall.core.search import Searcher

    db_path = _resolve_db_path()
    if not db_path.exists():
        return {
            "ok": False,
            "error": "db_not_found",
            "db": str(db_path),
            "hits": [],
        }

    try:
        with Searcher(db_path=db_path) as s:
            hits = s.search(
                query,
                mode="bm25",
                source=source,
                register=register,
                role=role,
                since=since,
                until=until,
                limit=top_k,
            )
            drawers = list(s.last_drawers)
    except FTS5QueryError as e:
        return {
            "ok": False,
            "error": "fts5_query_error",
            "detail": str(e),
            "hits": [],
        }
    except sqlite3.OperationalError as e:
        return {
            "ok": False,
            "error": "sqlite_error",
            "detail": str(e),
            "hits": [],
        }

    return {
        "ok": True,
        "query": query,
        "top_k": top_k,
        "hit_count": len(hits),
        "hits": [
            {
                "drawer_uid": hit.drawer_uid,
                "score": hit.score,
                "rank": hit.rank,
                "source": hit.source,
                "created_at": hit.created_at,
                "snippet": _strip_ansi(hit.snippet),
                "source_path": drawer.source_path,
                "role": drawer.role,
                "register": drawer.register,
                "thread_id": drawer.thread_id,
            }
            for hit, drawer in zip(hits, drawers, strict=False)
        ],
    }


def _do_recall_drawer(drawer_uid: str) -> dict[str, Any]:
    """Implementation backing the recall_drawer MCP tool.

    Accepts a full drawer_uid OR a unique prefix (git-short-SHA-style).
    Errors with disambiguation list when the prefix matches multiple.
    """
    try:
        conn = _open_conn()
    except FileNotFoundError as e:
        return {"ok": False, "error": "db_not_found", "detail": str(e)}

    try:
        try:
            full_uid = resolve_drawer_uid_prefix(drawer_uid, conn)
        except _DrawerNotFoundError:
            return {
                "ok": False,
                "error": "drawer_not_found",
                "input": drawer_uid,
            }
        except _AmbiguousPrefixError as e:
            return {
                "ok": False,
                "error": "ambiguous_prefix",
                "input": drawer_uid,
                "candidates": e.candidates,
            }

        # Pull metadata + content. The content lives in the FTS5 table;
        # drawer_meta carries the metadata. Join via rowid.
        row = conn.execute(
            "SELECT m.drawer_uid, m.source, m.source_id, m.source_path, m.role, "
            "       m.register, m.thread_id, m.parent_uid, m.position_in_thread, "
            "       m.created_at, m.content_hash, m.risk_score, "
            "       f.content "
            "FROM drawer_meta m "
            "LEFT JOIN drawers_fts f ON f.rowid = m.rowid "
            "WHERE m.drawer_uid = ?",
            (full_uid,),
        ).fetchone()
        if row is None:
            # Defensive — resolve found a uid but the row vanished. Rare.
            return {
                "ok": False,
                "error": "drawer_not_found",
                "input": drawer_uid,
            }

        return {
            "ok": True,
            "drawer_uid": row["drawer_uid"],
            "source": row["source"],
            "source_id": row["source_id"],
            "source_path": row["source_path"],
            "role": row["role"],
            "register": row["register"],
            "thread_id": row["thread_id"],
            "parent_uid": row["parent_uid"],
            "position_in_thread": row["position_in_thread"],
            "created_at": row["created_at"],
            "content_hash": row["content_hash"],
            "risk_score": row["risk_score"],
            "content": row["content"] or "",
        }
    finally:
        conn.close()


def _do_recall_status() -> dict[str, Any]:
    """Implementation backing the recall_status MCP tool.

    Mirrors `recall status --json` shape exactly: same keys, same types.
    """
    db_path = _resolve_db_path()
    if not db_path.exists():
        return {
            "ok": False,
            "db": str(db_path),
            "error": "db_not_found",
        }

    payload: dict[str, Any] = {
        "ok": True,
        "db": str(db_path),
        "db_size_bytes": db_path.stat().st_size,
        "schema_version": None,
        "schema_applied_at": None,
        "wal_size_pages": None,
        "drawers_total": 0,
        "drawers_by_source": {},
        "last_indexed_at": None,
        "ingest_errors": 0,
    }

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            sv_row = conn.execute(
                "SELECT MAX(version) AS v, MAX(applied_at) AS at "
                "FROM schema_version WHERE status = 'applied'"
            ).fetchone()
            if sv_row and sv_row["v"] is not None:
                payload["schema_version"] = int(sv_row["v"])
                payload["schema_applied_at"] = (
                    int(sv_row["at"]) if sv_row["at"] is not None else None
                )
        except sqlite3.OperationalError:
            pass

        try:
            wal_row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if wal_row is not None:
                payload["wal_size_pages"] = int(wal_row[1])
        except sqlite3.OperationalError:
            pass

        try:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM drawer_meta "
                "GROUP BY source ORDER BY n DESC"
            ).fetchall()
            by_source = {r["source"]: int(r["n"]) for r in rows}
            payload["drawers_by_source"] = by_source
            payload["drawers_total"] = sum(by_source.values())
        except sqlite3.OperationalError as e:
            payload["drawers_error"] = str(e)

        try:
            row = conn.execute(
                "SELECT MAX(last_indexed_mtime) AS at FROM index_state"
            ).fetchone()
            if row and row["at"]:
                payload["last_indexed_at"] = int(row["at"])
        except sqlite3.OperationalError:
            pass

        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM ingest_errors").fetchone()
            payload["ingest_errors"] = int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    return payload


def _do_recall_graph_query(
    entity_name: str,
    *,
    relationship_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Implementation backing the recall_graph_query MCP tool.

    Looks up entities by name (case-insensitive). For each match, returns
    the drawer mentions and (optionally filtered) outgoing relationships.
    """
    try:
        conn = _open_conn()
    except FileNotFoundError as e:
        return {"ok": False, "error": "db_not_found", "detail": str(e)}

    try:
        try:
            ent_rows = conn.execute(
                "SELECT id, name, type FROM entities WHERE LOWER(name) = LOWER(?)",
                (entity_name,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            return {"ok": False, "error": "entities_unavailable", "detail": str(e)}

        if not ent_rows:
            return {
                "ok": True,
                "entity_name": entity_name,
                "matches": [],
            }

        matches: list[dict[str, Any]] = []
        for ent in ent_rows:
            mentions = conn.execute(
                "SELECT m.drawer_uid, m.confidence, m.detected_by, "
                "       d.source, d.created_at "
                "FROM drawer_entity_mentions m "
                "JOIN drawer_meta d ON d.drawer_uid = m.drawer_uid "
                "WHERE m.entity_id = ? "
                "ORDER BY m.detected_at DESC, m.drawer_uid ASC "
                "LIMIT ?",
                (ent["id"], limit),
            ).fetchall()

            if relationship_type:
                rels = conn.execute(
                    "SELECT r.predicate, e2.name AS other_name, e2.type AS other_type, "
                    "       r.drawer_uid, r.valid_from, r.valid_to "
                    "FROM relationships r "
                    "JOIN entities e2 ON e2.id = r.object_id "
                    "WHERE r.subject_id = ? AND r.predicate = ? "
                    "ORDER BY e2.name "
                    "LIMIT ?",
                    (ent["id"], relationship_type, limit),
                ).fetchall()
            else:
                rels = conn.execute(
                    "SELECT r.predicate, e2.name AS other_name, e2.type AS other_type, "
                    "       r.drawer_uid, r.valid_from, r.valid_to "
                    "FROM relationships r "
                    "JOIN entities e2 ON e2.id = r.object_id "
                    "WHERE r.subject_id = ? "
                    "ORDER BY r.predicate, e2.name "
                    "LIMIT ?",
                    (ent["id"], limit),
                ).fetchall()

            matches.append(
                {
                    "id": ent["id"],
                    "name": ent["name"],
                    "type": ent["type"],
                    "mentions": [
                        {
                            "drawer_uid": m["drawer_uid"],
                            "confidence": m["confidence"],
                            "detected_by": m["detected_by"],
                            "source": m["source"],
                            "created_at": m["created_at"],
                        }
                        for m in mentions
                    ],
                    "relationships": [
                        {
                            "predicate": r["predicate"],
                            "object_name": r["other_name"],
                            "object_type": r["other_type"],
                            "drawer_uid": r["drawer_uid"],
                            "valid_from": r["valid_from"],
                            "valid_to": r["valid_to"],
                        }
                        for r in rels
                    ],
                }
            )

        return {
            "ok": True,
            "entity_name": entity_name,
            "relationship_type": relationship_type,
            "matches": matches,
        }
    finally:
        conn.close()


def _do_recall_forget(
    drawer_uid_prefix: str,
    *,
    dry_run: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Implementation backing the recall_forget MCP tool.

    Accepts a unique drawer_uid prefix; resolves to a full uid; soft-hides
    by inserting into ``hidden_drawers``. Idempotent: re-hiding a drawer
    is a no-op aside from updating ``hidden_at``.
    """
    try:
        conn = _open_conn()
    except FileNotFoundError as e:
        return {"ok": False, "error": "db_not_found", "detail": str(e)}

    try:
        try:
            full_uid = resolve_drawer_uid_prefix(drawer_uid_prefix, conn)
        except _DrawerNotFoundError:
            return {
                "ok": False,
                "error": "drawer_not_found",
                "input": drawer_uid_prefix,
            }
        except _AmbiguousPrefixError as e:
            return {
                "ok": False,
                "error": "ambiguous_prefix",
                "input": drawer_uid_prefix,
                "candidates": e.candidates,
            }

        if dry_run:
            meta = conn.execute(
                "SELECT source, role, register, created_at, source_path "
                "FROM drawer_meta WHERE drawer_uid = ?",
                (full_uid,),
            ).fetchone()
            return {
                "ok": True,
                "dry_run": True,
                "drawer_uid": full_uid,
                "source": meta["source"] if meta else None,
                "role": meta["role"] if meta else None,
                "register": meta["register"] if meta else None,
                "created_at": meta["created_at"] if meta else None,
                "source_path": meta["source_path"] if meta else None,
                "reason": reason,
            }

        now = int(datetime.now(tz=UTC).timestamp())
        conn.execute(_HIDDEN_DRAWERS_DDL)
        conn.execute(
            "INSERT INTO hidden_drawers (drawer_uid, hidden_at, unhidden_at, reason) "
            "VALUES (?, ?, NULL, ?) "
            "ON CONFLICT(drawer_uid) DO UPDATE SET "
            "  hidden_at = excluded.hidden_at, "
            "  unhidden_at = NULL, "
            "  reason = excluded.reason",
            (full_uid, now, reason),
        )
        conn.commit()
        return {
            "ok": True,
            "dry_run": False,
            "drawer_uid": full_uid,
            "hidden_at": now,
            "reason": reason,
        }
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# ----------------------------------------------------------------------
# MCP server registration
# ----------------------------------------------------------------------


def build_server() -> Any:
    """Construct the FastMCP server with all 5 tools registered.

    Imports ``mcp.server.fastmcp`` lazily so importing this module does
    not require the ``[mcp]`` extra to be installed. Tests that exercise
    the pure-Python `_do_*` functions don't need FastMCP.
    """
    from mcp.server.fastmcp import FastMCP

    server: Any = FastMCP(
        name="aurochs-recall",
        instructions=(
            "Memory architecture for AI conversations. Use `recall_search` "
            "for any open-ended search; `recall_drawer` to fetch verbatim "
            "drawer content by uid; `recall_graph_query` for entity/relationship "
            "lookups; `recall_status` for DB health checks; `recall_forget` "
            "to soft-delete a drawer (preserves audit trail; reversible)."
        ),
    )

    @server.tool(
        name="recall_search",
        description=(
            "Search the recall index (SQLite FTS5 BM25). Returns ranked drawer "
            "hits with snippets. Use `top_k` to size the result set; defaults "
            "to 10. Optional filters: `source` (list), `register`, `role`, "
            "`since`/`until` (epoch seconds)."
        ),
    )
    def recall_search(
        query: str,
        top_k: int = 10,
        source: list[str] | None = None,
        register: str | None = None,
        role: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> dict[str, Any]:
        return _do_recall_search(
            query,
            top_k=top_k,
            source=source,
            register=register,
            role=role,
            since=since,
            until=until,
        )

    @server.tool(
        name="recall_drawer",
        description=(
            "Fetch a full drawer by drawer_uid (or unique prefix, git-short-SHA "
            "style). Returns verbatim content, source metadata, thread context. "
            "Errors with disambiguation list if the prefix matches multiple."
        ),
    )
    def recall_drawer(drawer_uid: str) -> dict[str, Any]:
        return _do_recall_drawer(drawer_uid)

    @server.tool(
        name="recall_status",
        description=(
            "DB health snapshot: schema version, drawer counts by source, "
            "WAL size in pages, last index time, ingest error count. Useful "
            "for diagnosing index health from the LLM side without leaving "
            "the conversation."
        ),
    )
    def recall_status() -> dict[str, Any]:
        return _do_recall_status()

    @server.tool(
        name="recall_graph_query",
        description=(
            "Knowledge-graph entity lookup. Returns mentions and outgoing "
            "relationships for the named entity (case-insensitive). Pass "
            "`relationship_type` to filter relationships (e.g. AUTHORED_BY, "
            "USES, COLLABORATES_WITH)."
        ),
    )
    def recall_graph_query(
        entity_name: str,
        relationship_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return _do_recall_graph_query(
            entity_name,
            relationship_type=relationship_type,
            limit=limit,
        )

    @server.tool(
        name="recall_forget",
        description=(
            "Soft-delete a drawer. Hides from search; the drawer row is "
            "preserved for audit. Accepts unique drawer_uid prefix "
            "(git-short-SHA-style). Pass `dry_run=true` to preview the "
            "resolved drawer without writing."
        ),
    )
    def recall_forget(
        drawer_uid_prefix: str,
        dry_run: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return _do_recall_forget(
            drawer_uid_prefix, dry_run=dry_run, reason=reason
        )

    return server


def main() -> None:
    """Entrypoint for `python -m aurochs_recall.mcp.server`."""
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
