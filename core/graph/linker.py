"""Always-on seed-entity linker.

After every drawer write, the linker scans the content for the names and
aliases listed in the user's ``seed-entities.toml`` and writes
``MENTIONS`` relationships into the graph. It runs on the indexer's
worker thread (no LLM, no network — pure local string matching).

T0 simplifications
------------------
* No fuzzy matching — exact case-insensitive substring only.
* Word-boundary check is approximate: the match must be preceded and
  followed by a non-letter character (or string boundary). Catches
  ``Stefan.`` and ``Stefan,`` but not ``Stefan's`` (acceptable for T0).
* Aliases are flat — no hierarchical alias resolution.
* No entity-type assignment beyond what the seed file specifies; if the
  seed says ``Stefan`` is type ``person``, that's what we record.

The seed-entities config is a list of ``SeedEntity`` records; tests pass
them in directly, the CLI loads from TOML.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from core.graph.store import add_entity, link_entity_in_drawer
from core.types import Drawer


@dataclass(frozen=True, slots=True)
class SeedEntity:
    """A single line from the seed-entities config.

    ``aliases`` should NOT include the canonical name itself — the linker
    matches on canonical OR alias. ``type_`` must already exist in
    ``entity_types`` (the spine seeds the standard set).
    """

    name: str
    type_: str
    aliases: tuple[str, ...] = ()


def _build_pattern(seed: SeedEntity) -> re.Pattern[str]:
    """Compile a single regex matching the canonical name or any alias.

    Uses ``\\b``-style boundaries via lookarounds for unicode-friendliness.
    Case-insensitive. Variants are sorted longest-first so longer aliases
    win when they overlap a prefix of another (e.g. ``San Francisco`` wins
    over ``San``).
    """
    variants = sorted({seed.name, *seed.aliases}, key=len, reverse=True)
    escaped = "|".join(re.escape(v) for v in variants if v)
    # Surround with negative-lookbehind/ahead for letter-character. This is
    # the T0 approximation of \b that handles unicode names better than
    # the bytes-only \b pattern.
    body = rf"(?<![\w]){escaped}(?![\w])"
    return re.compile(body, flags=re.IGNORECASE)


class Linker:
    """Compile-once-scan-many seed-entity linker.

    Construct with the seed config; call :meth:`link_drawer` per drawer
    after it's been inserted into the database. The linker uses
    :func:`core.graph.store.add_entity` to ensure the entity exists,
    then :func:`link_entity_in_drawer` to create the relationship.
    """

    def __init__(self, seeds: Iterable[SeedEntity]) -> None:
        """Compile patterns for every seed entity at construction time."""
        self._seeds: tuple[SeedEntity, ...] = tuple(seeds)
        self._patterns: list[tuple[SeedEntity, re.Pattern[str]]] = [
            (s, _build_pattern(s)) for s in self._seeds
        ]

    def link_drawer(self, conn: sqlite3.Connection, drawer: Drawer) -> int:
        """Scan ``drawer.content`` for seed mentions; record relationships.

        Returns the number of new relationships created. Idempotent: the
        store layer dedups on (subject, predicate, object) implicitly via
        the relationships table — but T0 doesn't enforce that uniqueness,
        so callers should run the linker exactly once per drawer.
        """
        if not self._patterns:
            return 0
        content = drawer.content
        created = 0
        for seed, pattern in self._patterns:
            if pattern.search(content) is None:
                continue
            entity = add_entity(
                conn,
                seed.name,
                seed.type_,
                source="seed",
            )
            link_entity_in_drawer(
                conn,
                drawer_uid=drawer.drawer_uid,
                entity_id=entity.id,
                predicate="MENTIONS",
            )
            created += 1
        return created
