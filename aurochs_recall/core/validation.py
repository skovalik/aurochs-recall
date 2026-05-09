"""Input validation gate.

Single module routing all user input through validators per plan v5.
Every ingestor and CLI surface should call into here rather than rolling
its own checks. Keeps the rules in one place and one place only.

``normalize_whitespace`` and ``compute_content_hash`` live on
:mod:`core.types` (the spine owns them so the dataclass and the
validators can't drift apart). They're re-exported here so callers that
import from ``core.validation`` find them in the obvious place.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Literal

# Re-export from the spine so there's exactly one definition each.
from .types import compute_content_hash, normalize_whitespace

# ----- Public API ---------------------------------------------------------


class InvalidInput(ValueError):
    """Raised when validation rejects input. Caller should turn this into
    a user-facing error (CLI exit 2, MCP error response, etc.) rather
    than a stack trace."""


__all__ = [
    "InvalidInput",
    "compute_content_hash",
    "normalize_whitespace",
    "validate_entity_name",
    "validate_file_path",
    "validate_predicate_name",
    "validate_query_string",
]

# Sentinel strings that show up in user data but mean "no value." We refuse
# to store these as entity names because they collide with NULL semantics
# in human-written queries.
_NAME_SENTINELS = frozenset({"null", "none", "undefined"})

_PREDICATE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Windows reserved names (case-insensitive, applies to stem only).
_WIN_RESERVED_FIXED = frozenset({"CON", "NUL", "AUX", "PRN"})
_WIN_RESERVED_NUMBERED = re.compile(r"^(COM|LPT)[1-9]$")


def validate_entity_name(name: str) -> str:
    """Normalize and validate a knowledge-graph entity name.

    Rejects empty / whitespace-only and the sentinel strings ``null``,
    ``none``, ``undefined`` (case-insensitive). Returns NFC-normalized
    text so equivalent unicode forms collapse to one canonical entry.
    """
    if not isinstance(name, str):
        raise InvalidInput(f"Entity name must be str, got {type(name).__name__}")
    stripped = name.strip()
    if not stripped:
        raise InvalidInput("Entity name cannot be empty")
    if stripped.lower() in _NAME_SENTINELS:
        raise InvalidInput(f"Entity name cannot be sentinel: {stripped!r}")
    return unicodedata.normalize("NFC", stripped)


def validate_query_string(
    query: str,
    mode: Literal["literal", "fts5_raw"] = "literal",
) -> str:
    """Prepare a query for FTS5 MATCH.

    ``literal`` (default): the entire query is wrapped in quotes and any
    embedded quotes doubled. This makes FTS5 treat the input as a phrase
    rather than parsing it as MATCH syntax. Safe for arbitrary user text
    including parens, ``OR``, ``NEAR``, etc.

    ``fts5_raw``: pass-through. Caller has explicitly opted in to MATCH
    syntax via ``--raw`` and accepts the responsibility of producing
    valid FTS5.
    """
    if not isinstance(query, str):
        raise InvalidInput(f"Query must be str, got {type(query).__name__}")
    if mode == "literal":
        return '"' + query.replace('"', '""') + '"'
    if mode == "fts5_raw":
        return query
    raise InvalidInput(f"Unknown query mode: {mode!r}")


def validate_file_path(path: Path | str) -> Path:
    """Reject paths with null bytes or Windows reserved component names.

    Always returns a ``Path``, but does NOT resolve / canonicalize — the
    caller decides whether to ``resolve()`` based on whether they need
    the path to actually exist.
    """
    if isinstance(path, str):
        path = Path(path)
    s = str(path)
    if "\x00" in s:
        raise InvalidInput("Path contains null byte")
    if sys.platform == "win32":
        for component in path.parts:
            stem = component.split(".")[0].upper()
            if not stem:
                continue
            if stem in _WIN_RESERVED_FIXED or _WIN_RESERVED_NUMBERED.match(stem):
                raise InvalidInput(
                    f"Path contains Windows reserved name: {component!r}"
                )
    return path


def validate_predicate_name(pred: str) -> str:
    """Validate a knowledge-graph predicate name.

    Convention: uppercase snake (``WORKS_FOR``, ``MENTIONED_BY``, etc.).
    Enforced by regex so the taxonomy stays consistent and predicates
    can be substring-searched without ambiguity.
    """
    if not isinstance(pred, str):
        raise InvalidInput(f"Predicate must be str, got {type(pred).__name__}")
    if not _PREDICATE_RE.match(pred):
        raise InvalidInput(
            f"Predicate must match /^[A-Z][A-Z0-9_]*$/: {pred!r}"
        )
    return pred
