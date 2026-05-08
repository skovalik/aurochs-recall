"""Unit tests for the graph store + linker."""

from __future__ import annotations

import time
from pathlib import Path

from core.db import connect
from core.graph.linker import Linker, SeedEntity
from core.graph.store import (
    add_entity,
    link_entity_in_drawer,
    list_entities_for_drawer,
    query_entity,
)
from core.migrations.runner import run_migrations
from core.types import Drawer


def _setup(tmp_path: Path) -> Path:
    db = tmp_path / "recall.db"
    run_migrations(db)
    return db


# ---------------------------------------------------------------------------
# add_entity / query_entity
# ---------------------------------------------------------------------------

class TestAddEntity:
    def test_inserts_new(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            e = add_entity(conn, "Stefan Kovalik", "person")
            assert e.id > 0
            assert e.name == "Stefan Kovalik"
            assert e.type == "person"
            assert e.source == "seed"
        finally:
            conn.close()

    def test_get_or_create(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            e1 = add_entity(conn, "Stefan", "person")
            e2 = add_entity(conn, "Stefan", "person")
            assert e1.id == e2.id  # same row, get-or-create semantics
        finally:
            conn.close()

    def test_case_insensitive_dedup(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            e1 = add_entity(conn, "Stefan", "person")
            e2 = add_entity(conn, "stefan", "person")
            e3 = add_entity(conn, "STEFAN", "person")
            assert e1.id == e2.id == e3.id
        finally:
            conn.close()

    def test_type_distinguishes(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            person = add_entity(conn, "Cognograph", "person")
            project = add_entity(conn, "Cognograph", "project")
            assert person.id != project.id
        finally:
            conn.close()


class TestQueryEntity:
    def test_finds(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            add_entity(conn, "Stefan", "person")
            results = query_entity(conn, "Stefan")
            assert len(results) == 1
            assert results[0].name == "Stefan"
        finally:
            conn.close()

    def test_empty_for_unknown(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            assert query_entity(conn, "Nobody") == []
        finally:
            conn.close()

    def test_filter_by_type(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            add_entity(conn, "Cognograph", "person")
            add_entity(conn, "Cognograph", "project")
            persons = query_entity(conn, "Cognograph", type_="person")
            assert len(persons) == 1
            assert persons[0].type == "person"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# link_entity_in_drawer
# ---------------------------------------------------------------------------

class TestLinkEntityInDrawer:
    def test_creates_relationship(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            # Insert a drawer first so the FK target exists.
            drawer = Drawer(
                source="claude_code",
                source_id="s:0",
                role="human",
                content="Stefan likes Cognograph",
                created_at=int(time.time()),
            )
            conn.execute(
                "INSERT INTO drawer_meta "
                "(drawer_uid, source, source_id, role, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    drawer.drawer_uid,
                    drawer.source,
                    drawer.source_id,
                    drawer.role,
                    drawer.created_at,
                    drawer.content_hash,
                ),
            )

            stefan = add_entity(conn, "Stefan", "person")
            rel_id = link_entity_in_drawer(conn, drawer.drawer_uid, stefan.id)
            assert rel_id > 0

            row = conn.execute(
                "SELECT predicate, drawer_uid FROM relationships WHERE id = ?",
                (rel_id,),
            ).fetchone()
            assert row["predicate"] == "MENTIONS"
            assert row["drawer_uid"] == drawer.drawer_uid
        finally:
            conn.close()

    def test_list_entities_for_drawer(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            drawer = Drawer(
                source="markdown",
                source_id="path/to/file.md:0",
                role="wiki",
                content="Aurochs is a project; Stefan is the founder.",
                created_at=int(time.time()),
            )
            conn.execute(
                "INSERT INTO drawer_meta "
                "(drawer_uid, source, source_id, role, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    drawer.drawer_uid,
                    drawer.source,
                    drawer.source_id,
                    drawer.role,
                    drawer.created_at,
                    drawer.content_hash,
                ),
            )

            stefan = add_entity(conn, "Stefan", "person")
            aurochs = add_entity(conn, "Aurochs", "project")
            link_entity_in_drawer(conn, drawer.drawer_uid, stefan.id)
            link_entity_in_drawer(conn, drawer.drawer_uid, aurochs.id)

            linked = list_entities_for_drawer(conn, drawer.drawer_uid)
            names = {e.name for e in linked}
            assert names == {"Stefan", "Aurochs"}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Linker
# ---------------------------------------------------------------------------

class TestLinker:
    def test_matches_canonical(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            drawer = Drawer(
                source="markdown", source_id="f:0", role="wiki",
                content="Stefan is working on Aurochs today.",
                created_at=0,
            )
            conn.execute(
                "INSERT INTO drawer_meta "
                "(drawer_uid, source, source_id, role, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (drawer.drawer_uid, drawer.source, drawer.source_id,
                 drawer.role, drawer.created_at, drawer.content_hash),
            )

            linker = Linker([
                SeedEntity(name="Stefan", type_="person"),
                SeedEntity(name="Aurochs", type_="project"),
                SeedEntity(name="Cognograph", type_="project"),
            ])
            count = linker.link_drawer(conn, drawer)
            assert count == 2  # Stefan + Aurochs, not Cognograph

            linked_names = {e.name for e in list_entities_for_drawer(conn, drawer.drawer_uid)}
            assert linked_names == {"Stefan", "Aurochs"}
        finally:
            conn.close()

    def test_matches_alias(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            drawer = Drawer(
                source="markdown", source_id="f:0", role="wiki",
                content="PFD is the methodology I use.",
                created_at=0,
            )
            conn.execute(
                "INSERT INTO drawer_meta "
                "(drawer_uid, source, source_id, role, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (drawer.drawer_uid, drawer.source, drawer.source_id,
                 drawer.role, drawer.created_at, drawer.content_hash),
            )

            linker = Linker([
                SeedEntity(
                    name="Perception-First Design",
                    type_="methodology",
                    aliases=("PFD",),
                ),
            ])
            count = linker.link_drawer(conn, drawer)
            assert count == 1

            linked = list_entities_for_drawer(conn, drawer.drawer_uid)
            assert len(linked) == 1
            assert linked[0].name == "Perception-First Design"
        finally:
            conn.close()

    def test_word_boundary_does_not_match_substring(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            drawer = Drawer(
                source="markdown", source_id="f:0", role="wiki",
                content="The catastrophe was avoided by careful planning.",
                created_at=0,
            )
            conn.execute(
                "INSERT INTO drawer_meta "
                "(drawer_uid, source, source_id, role, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (drawer.drawer_uid, drawer.source, drawer.source_id,
                 drawer.role, drawer.created_at, drawer.content_hash),
            )

            # "cat" should NOT match "catastrophe"
            linker = Linker([SeedEntity(name="cat", type_="concept")])
            assert linker.link_drawer(conn, drawer) == 0
        finally:
            conn.close()

    def test_case_insensitive(self, tmp_path: Path) -> None:
        db = _setup(tmp_path)
        conn = connect(db)
        try:
            drawer = Drawer(
                source="markdown", source_id="f:0", role="wiki",
                content="STEFAN said it was fine.",
                created_at=0,
            )
            conn.execute(
                "INSERT INTO drawer_meta "
                "(drawer_uid, source, source_id, role, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (drawer.drawer_uid, drawer.source, drawer.source_id,
                 drawer.role, drawer.created_at, drawer.content_hash),
            )

            linker = Linker([SeedEntity(name="Stefan", type_="person")])
            count = linker.link_drawer(conn, drawer)
            assert count == 1
        finally:
            conn.close()
