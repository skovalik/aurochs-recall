"""Ingestor protocol + shared helpers.

The Ingestor contract is intentionally tiny: ``can_handle(path)`` decides
whether this ingestor wants to look at a path; ``extract(path)`` yields
``Drawer`` objects from it. The indexer is responsible for orchestrating
mtime checks, pooling, error logging, and storage — ingestors only do
parsing and normalization.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..types import Drawer


class IngestError(Exception):
    """Raised when a file cannot be parsed at all (vs. a single bad line,
    which the ingestor logs and skips)."""


@runtime_checkable
class Ingestor(Protocol):
    """Protocol every ingestor implements.

    Implementations should be effectively stateless — the indexer creates
    one instance and feeds it many paths. Anything per-file (line count,
    schema version detected, etc.) belongs in metadata on the Drawer or in
    a logging side-channel.
    """

    name: str

    def can_handle(self, path: Path) -> bool:
        """Return True iff this ingestor is willing to parse ``path``."""
        ...

    def extract(self, path: Path) -> Iterator[Drawer]:
        """Yield drawers extracted from ``path``.

        Implementations MUST:
        * skip but log (don't raise) on per-record parse errors
        * filter out content shorter than ``MIN_CONTENT_LEN``
        * filter out pure-whitespace messages
        * filter out slash-only commands (e.g. ``/screenshot``)
        * raise ``IngestError`` only when the file is unrecoverable
          (corrupt JSON top-level, missing required keys at envelope
          level, etc.)
        """
        ...


# ----- Filter constants ---------------------------------------------------

# Content shorter than this is treated as noise. Threshold from plan v5:
# "skip messages <30 chars". Matches typical agent acknowledgements
# ("ok.", "thanks", "next") without dropping substantive one-liners.
MIN_CONTENT_LEN = 30


# ----- Shared filter helper -----------------------------------------------


def should_skip_content(content: str) -> bool:
    """Apply the standard content filters: too short, whitespace-only,
    or a slash-command. Returns True if the drawer should be dropped.

    Centralized here so all three ingestors apply identical rules and
    test fixtures stay in sync with reality.
    """
    if not isinstance(content, str):
        return True
    stripped = content.strip()
    if not stripped:
        return True
    if len(stripped) < MIN_CONTENT_LEN:
        return True
    # Slash-only command: starts with `/`, contains no whitespace except
    # leading/trailing. Matches `/screenshot`, `/stefan capture-only`,
    # etc. Allows multi-token slash commands through if they have a body
    # past the command word, which is the common case for /-commands
    # that do real work and should be indexed.
    if stripped.startswith("/"):
        # split off the command word; anything after counts as content
        parts = stripped.split(maxsplit=1)
        if len(parts) == 1:
            return True
        # has a body — but if the body is shorter than threshold, still skip
        body = parts[1].strip()
        if len(body) < MIN_CONTENT_LEN:
            return True
    return False


# ----- Encoding helpers ---------------------------------------------------


def read_text_with_fallback(
    path: Path,
    primary: str = "utf-8",
    fallback: str = "latin-1",
) -> str:
    """Read a text file with utf-8 first, falling back to latin-1.

    Caller is the ingestor; we keep this generic so each ingestor can
    decide its own primary encoding (markdown defaults utf-8; legacy
    Claude Code jsonl on Windows sometimes lands as cp1252).

    NOTE: latin-1 NEVER fails decode — every byte maps to a codepoint —
    so it's a guaranteed fallback. We only use it after a real utf-8
    failure so we don't silently mojibake utf-8 content as latin-1.
    """
    try:
        return path.read_text(encoding=primary)
    except UnicodeDecodeError:
        # Try chardet if available; otherwise straight to latin-1.
        try:
            import chardet  # type: ignore

            raw = path.read_bytes()
            detected = chardet.detect(raw)
            encoding = detected.get("encoding") or fallback
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                return raw.decode(fallback, errors="replace")
        except ImportError:
            return path.read_text(encoding=fallback, errors="replace")


def strip_bom(text: str) -> str:
    """Strip a leading byte-order mark if present.

    UTF-8 BOM (``\\ufeff``) and UTF-16 BOMs both round-trip through
    ``read_text`` as a leading ``\\ufeff`` codepoint. Removing it here
    means the rest of the parser sees clean text.
    """
    if text.startswith("﻿"):
        return text[1:]
    return text
