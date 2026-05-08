"""Basic graph CRUD against the sqlite-only T0 backend.

Kuzu and other graph engines are deferred to ``[graph]`` extra; the T0
spine reads/writes ``entities``, ``relationships``, and the
``drawer_entity_mentions`` join table directly. The API mirrors what plan
v4's ``core/graph/store.py`` describes.

Two distinct edge categories live here:

* **Entity↔entity edges** go in ``relationships``. ``subject_id`` and
  ``object_id`` both point into ``entities`` and the table's
  ``CHECK(subject_id != object_id)`` rules out self-loops. Use this for
  predicates like ``AUTHORED_BY``, ``USES``, ``LOCATED_IN``.
* **Drawer→entity mentions** go in ``drawer_entity_mentions``. Drawers
  aren't entities, so they can't appear on either side of a relationship
  edge. The join table records "drawer X mentions entity Y" with a
  ``detected_by`` provenance column (``linker | extractor | manual``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from core.types import Entity, Relationship


def add_entity(
    conn: sqlite3.Connection,
    name: str,
    type_: str,
    *,
    metadata: dict[str, Any] | None = None,
    source: str = "seed",
    timestamp: int | None = None,
) -> Entity:
    """Insert or fetch an entity by canonical (LOWER(name), type).

    The UNIQUE INDEX ``idx_entities_canonical`` enforces case-insensitive
    deduplication. If a row with the same (LOWER(name), type) exists it's
    returned unchanged — this is the get-or-create pattern.

    Parameters
    ----------
    name:
        Display name. Must be non-empty and not a NULL-sentinel.
    type_:
        Entity type — must already exist in ``entity_types``. Use the
        seeded set (``person | project | concept | event | tool |
        methodology | place``) or call ``add_entity_type`` first.
    metadata:
        JSON-serializable per-entity properties. ``None`` becomes ``NULL``.
    source:
        Provenance — one of ``seed | llm | manual``.
    """
    now = timestamp if timestamp is not None else int(time.time())
    metadata_json = json.dumps(metadata) if metadata else None

    # Try INSERT; if the canonical UNIQUE INDEX rejects, fetch the existing.
    cursor = conn.execute(
        "INSERT OR IGNORE INTO entities "
        "(name, type, metadata, first_seen, last_seen, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, type_, metadata_json, now, now, source),
    )
    if cursor.rowcount:
        entity_id = cursor.lastrowid or 0
        return Entity(
            id=entity_id,
            name=name,
            type=type_,
            metadata=metadata or {},
            first_seen=now,
            last_seen=now,
            source=source,
        )

    # Existing row — fetch and return.
    row = conn.execute(
        "SELECT id, name, type, metadata, first_seen, last_seen, source "
        "FROM entities WHERE LOWER(name) = LOWER(?) AND type = ?",
        (name, type_),
    ).fetchone()
    if row is None:
        # Should be impossible — the INSERT OR IGNORE only short-circuits
        # on a real collision. Fail loud.
        raise RuntimeError(
            f"add_entity: insert short-circuited but no row found for "
            f"({name!r}, {type_!r})"
        )

    # Bump last_seen for this entity since we observed it again.
    conn.execute(
        "UPDATE entities SET last_seen = ? WHERE id = ?",
        (now, row["id"]),
    )

    parsed_meta: dict[str, Any] = {}
    if row["metadata"]:
        try:
            parsed_meta = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            parsed_meta = {}
    return Entity(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        metadata=parsed_meta,
        first_seen=row["first_seen"],
        last_seen=now,
        source=row["source"],
    )


def query_entity(
    conn: sqlite3.Connection,
    name: str,
    *,
    type_: str | None = None,
) -> list[Entity]:
    """Return all entities matching ``name`` (case-insensitive).

    If ``type_`` is given, restrict to that type. Returns an empty list
    when no match — never raises ``KeyError``.
    """
    if type_ is None:
        rows = conn.execute(
            "SELECT id, name, type, metadata, first_seen, last_seen, source "
            "FROM entities WHERE LOWER(name) = LOWER(?)",
            (name,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, type, metadata, first_seen, last_seen, source "
            "FROM entities WHERE LOWER(name) = LOWER(?) AND type = ?",
            (name, type_),
        ).fetchall()

    out: list[Entity] = []
    for row in rows:
        meta: dict[str, Any] = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError):
                meta = {}
        out.append(
            Entity(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                metadata=meta,
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                source=row["source"],
            )
        )
    return out


def link_drawer_to_entity(
    conn: sqlite3.Connection,
    drawer_uid: str,
    entity_id: int,
    *,
    confidence: float = 1.0,
    detected_by: str = "linker",
    detected_at: int | None = None,
) -> bool:
    """Record that ``drawer_uid`` mentions ``entity_id``.

    Writes to the ``drawer_entity_mentions`` join table. The (drawer_uid,
    entity_id) primary key is the dedup point — re-linking the same pair
    is idempotent.

    Parameters
    ----------
    drawer_uid:
        Citation target. Must already exist in ``drawer_meta``.
    entity_id:
        FK into ``entities``. Must already exist (use ``add_entity`` first).
    confidence:
        0.0–1.0. ``linker`` always uses 1.0; future ``extractor`` paths
        may emit a lower value when an LLM is uncertain.
    detected_by:
        Provenance: ``linker | extractor | manual``.
    detected_at:
        Epoch seconds. Defaults to ``time.time()``.

    Returns ``True`` if a new row was inserted, ``False`` if the mention
    was already on file (idempotent re-link).
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0.0, 1.0], got {confidence!r}")
    if detected_by not in ("linker", "extractor", "manual"):
        raise ValueError(
            f"detected_by must be one of linker | extractor | manual, "
            f"got {detected_by!r}"
        )
    now = detected_at if detected_at is not None else int(time.time())
    cursor = conn.execute(
        "INSERT OR IGNORE INTO drawer_entity_mentions "
        "(drawer_uid, entity_id, confidence, detected_by, detected_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (drawer_uid, entity_id, confidence, detected_by, now),
    )
    return cursor.rowcount > 0


def link_entity_in_drawer(
    conn: sqlite3.Connection,
    drawer_uid: str,
    entity_id: int,
    *,
    confidence: float = 1.0,
    detected_by: str = "linker",
    detected_at: int | None = None,
    # Legacy kwargs accepted but ignored — they were used by the synthetic-
    # entity workaround that this function replaces. Kept here so a stale
    # caller doesn't blow up; future patches can drop them.
    predicate: str | None = None,
    metadata: dict[str, Any] | None = None,
    valid_from: int | None = None,
) -> bool:
    """Compatibility alias for :func:`link_drawer_to_entity`.

    Earlier T0 prototypes split these two functions because the schema
    forced a workaround. Plan v5 collapses them into one — this name
    survives because it reads more naturally at the call site (the seed
    linker says "link this entity in this drawer"), and because external
    code may already import it.

    Returns ``True`` on first insert, ``False`` if the mention already
    existed (idempotent).

    The ``predicate``, ``metadata``, and ``valid_from`` kwargs are
    accepted but ignored — those concepts belong on entity↔entity edges,
    not on drawer mentions. They will be removed once T0 stabilizes.
    """
    del predicate, metadata, valid_from  # legacy kwargs, intentionally ignored
    return link_drawer_to_entity(
        conn,
        drawer_uid,
        entity_id,
        confidence=confidence,
        detected_by=detected_by,
        detected_at=detected_at,
    )


def list_entities_for_drawer(
    conn: sqlite3.Connection,
    drawer_uid: str,
) -> list[Entity]:
    """Return every entity linked to ``drawer_uid`` via the mentions table.

    Joins ``drawer_entity_mentions`` to ``entities`` so callers get fully
    hydrated ``Entity`` objects in deterministic name order.
    """
    rows = conn.execute(
        "SELECT e.id, e.name, e.type, e.metadata, "
        "       e.first_seen, e.last_seen, e.source "
        "FROM entities e "
        "JOIN drawer_entity_mentions m ON m.entity_id = e.id "
        "WHERE m.drawer_uid = ? "
        "ORDER BY e.name",
        (drawer_uid,),
    ).fetchall()

    out: list[Entity] = []
    for row in rows:
        meta: dict[str, Any] = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError):
                meta = {}
        out.append(
            Entity(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                metadata=meta,
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                source=row["source"],
            )
        )
    return out


def list_drawers_for_entity(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    limit: int | None = None,
) -> list[str]:
    """Return drawer_uids that mention ``entity_id``.

    Ordered by ``detected_at`` descending (newest first). Pass ``limit``
    to cap the result count — useful for the CLI's ``recall graph
    entity NAME --limit N`` path.
    """
    sql = (
        "SELECT drawer_uid FROM drawer_entity_mentions "
        "WHERE entity_id = ? "
        "ORDER BY detected_at DESC, drawer_uid ASC"
    )
    params: tuple[Any, ...] = (entity_id,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (entity_id, int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [row["drawer_uid"] for row in rows]
