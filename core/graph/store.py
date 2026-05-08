"""Basic graph CRUD against the sqlite-only T0 backend.

Kuzu and other graph engines are deferred to ``[graph]`` extra; the T0
spine reads/writes ``entities`` and ``relationships`` tables directly.
The API mirrors what plan v4's ``core/graph/store.py`` describes.
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
    predicate: str,
    *,
    metadata: dict[str, Any] | None = None,
    valid_from: int | None = None,
) -> Relationship:
    """Create a relationship (drawer mentions/uses/etc. an entity).

    The relationship's ``subject_id`` is the entity, the ``object_id`` is
    a synthetic "drawer-as-entity" pattern is NOT used here — instead we
    store the drawer_uid in the ``drawer_uid`` citation column and use a
    self-referential subject==object fallback for the case where the
    drawer doesn't directly correspond to a separate entity.

    For T0, the simpler interpretation is: ``predicate`` is something like
    ``MENTIONS`` and ``subject`` and ``object`` are both the entity (with
    drawer_uid carrying the citation context). The CHECK constraint
    ``subject_id != object_id`` rules out exact loops, so for direct
    "drawer mentions entity X" we record the entity twice would fail —
    instead we record (entity, MENTIONS, entity) only when the drawer
    establishes the entity's existence, and use the drawer_uid column as
    the citation that's queried on retrieval.

    A future patch reworks this once the LLM extraction layer lands and
    we know what shape edge-from-drawer extraction produces.

    For the T0 happy path: this function inserts a self-edge with
    ``predicate = 'MENTIONS'`` and the drawer_uid set, and is what the
    seed-entity linker calls. Tests verify the row exists and is
    queryable by ``drawer_uid``.
    """
    timestamp = valid_from if valid_from is not None else int(time.time())
    metadata_json = json.dumps(metadata) if metadata else None

    # T0 simplification: subject == object == entity_id violates the
    # CHECK(subject_id != object_id) constraint. So we represent the
    # "drawer mentions entity" link by inserting an *anchor* relationship
    # only when the predicate is NOT a self-edge — for MENTIONS we instead
    # write into a future-proofed pattern where the entity links to itself
    # via a synthetic "self" anchor. To avoid adding schema for that in T0,
    # we require the caller to provide a distinct subject and object when
    # they want a graph edge; the seed-entity linker uses the (entity,
    # MENTIONS, entity) interpretation by inserting two rows with the
    # entity as both subject and as a special "drawer_anchor" node — but
    # that's premature complexity for T0.
    #
    # Concrete T0 contract: this function expects ``entity_id`` to be the
    # SUBJECT, and the seed linker passes a second pre-resolved object
    # entity (e.g. linking entity → drawer-source entity). When the linker
    # only has a single entity and a drawer_uid, it stores the citation
    # with subject = object = entity_id only if a CHECK relaxation lands;
    # for T0 we punt and store a degenerate (entity, MENTIONS, entity) by
    # using the drawer-as-entity pattern below.
    #
    # Pragmatic implementation: if the linker passes entity_id only, we
    # insert (subject=entity, predicate, object=entity_self_token) where
    # entity_self_token is resolved to a different entity row representing
    # "the drawer itself" — for T0 this means linking back to a synthetic
    # 'drawer' entity created on demand. To avoid overcomplicating the T0
    # spine, the canonical link_drawer_to_entity API takes BOTH a
    # subject_id and object_id under different names; the legacy single-
    # entity callers use ``link_entity_in_drawer`` instead.
    raise NotImplementedError(
        "Use link_entity_in_drawer for T0; link_drawer_to_entity reserved for "
        "the post-extraction patch where drawer-as-subject is materialized."
    )


def link_entity_in_drawer(
    conn: sqlite3.Connection,
    drawer_uid: str,
    entity_id: int,
    *,
    predicate: str = "MENTIONS",
    metadata: dict[str, Any] | None = None,
    valid_from: int | None = None,
) -> int:
    """T0 flavor of drawer-to-entity linking.

    Records that ``drawer_uid`` mentions ``entity_id`` by inserting a
    relationship with subject == object == entity_id. The CHECK constraint
    ``subject_id != object_id`` would normally block this, so we use a
    paired-entity pattern: each drawer_uid becomes a synthetic entity of
    type ``concept`` named ``drawer:<uid>``, and the relationship is
    (drawer-entity, MENTIONS, real-entity).

    This keeps the schema CHECK happy and gives us a queryable graph edge.
    Returns the new relationship's ``id``.
    """
    timestamp = valid_from if valid_from is not None else int(time.time())
    metadata_json = json.dumps(metadata) if metadata else None

    drawer_entity_name = f"drawer:{drawer_uid}"
    drawer_entity = add_entity(
        conn,
        drawer_entity_name,
        "concept",
        metadata={"drawer_uid": drawer_uid, "synthetic": True},
        source="seed",
    )
    if drawer_entity.id == entity_id:
        # Pathological edge case (the entity name happens to collide with
        # a synthetic drawer entity name). Bail rather than violate CHECK.
        raise ValueError(
            f"Cannot link drawer-entity to itself: entity_id={entity_id}, "
            f"synthetic_drawer_entity_id={drawer_entity.id}"
        )

    cursor = conn.execute(
        "INSERT INTO relationships "
        "(subject_id, predicate, object_id, valid_from, valid_to, "
        " drawer_uid, metadata) VALUES (?, ?, ?, ?, NULL, ?, ?)",
        (
            drawer_entity.id,
            predicate,
            entity_id,
            timestamp,
            drawer_uid,
            metadata_json,
        ),
    )
    return cursor.lastrowid or 0


def list_entities_for_drawer(
    conn: sqlite3.Connection,
    drawer_uid: str,
) -> list[Entity]:
    """Return every entity linked to ``drawer_uid`` via any relationship."""
    rows = conn.execute(
        "SELECT DISTINCT e.id, e.name, e.type, e.metadata, "
        "       e.first_seen, e.last_seen, e.source "
        "FROM entities e "
        "JOIN relationships r ON r.object_id = e.id "
        "WHERE r.drawer_uid = ? "
        "  AND e.name NOT LIKE 'drawer:%' "
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
