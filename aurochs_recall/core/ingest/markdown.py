"""Markdown corpus ingestor.

Walks a directory recursively, finds ``.md`` files, and emits drawers.
Long files are chunked so each drawer is reasonably-sized for FTS5
results and reranking. Short files become a single drawer.

Per plan v5:
* ``thread_id`` = file path relative to the source root (forward-slashed)
* ``parent_uid`` = ``None`` (markdown has no thread structure)
* ``position_in_thread`` = first line number of the chunk (1-based)
* ``role`` = ``wiki`` if the path contains a ``wiki/`` segment,
  ``memory`` if it contains ``memory/``, else ``wiki`` (default)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from ..types import Drawer
from ..validation import compute_content_hash
from ._base import IngestError, read_text_with_fallback, should_skip_content, strip_bom

logger = logging.getLogger(__name__)

# Chunking parameters per plan v5: ~50-line chunks with 5-line overlap.
# Short enough that a chunk fits comfortably in a reranker context window;
# long enough that paragraph context is rarely sliced.
DEFAULT_CHUNK_SIZE = 50
DEFAULT_CHUNK_OVERLAP = 5
LONG_FILE_THRESHOLD = 1000  # files larger than this get chunked


class MarkdownIngestor:
    """Parses a directory tree of markdown files into Drawers.

    Constructor parameters let callers tune chunking for their corpus,
    but the defaults match plan v5 directly.
    """

    name: str = "markdown"

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        long_threshold: int = LONG_FILE_THRESHOLD,
        source_root: Path | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must be non-negative")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.long_threshold = long_threshold
        # source_root lets callers control how thread_id paths are
        # rendered (relative-to-corpus rather than relative-to-cwd).
        self.source_root = source_root

    # ----- Ingestor protocol ----------------------------------------------

    def can_handle(self, path: Path) -> bool:
        """Accept ``.md`` and ``.markdown`` files; reject other suffixes."""
        return path.suffix.lower() in {".md", ".markdown"}

    def extract(self, path: Path) -> Iterator[Drawer]:
        """Yield drawers parsed from a single markdown file."""
        try:
            text = read_text_with_fallback(path, primary="utf-8", fallback="latin-1")
        except OSError as e:
            raise IngestError(f"Failed to read {path}: {e}") from e

        text = strip_bom(text)
        lines = text.splitlines()
        if not lines:
            return

        mtime = int(path.stat().st_mtime)
        rel_path = self._relative_path(path)
        role = self._infer_role(rel_path)

        # Short file: single drawer covering the whole file.
        if len(lines) <= self.long_threshold:
            content = "\n".join(lines)
            if should_skip_content(content):
                return
            yield self._build_drawer(
                content=content,
                first_lineno=1,
                role=role,
                rel_path=rel_path,
                path=path,
                created_at=mtime,
            )
            return

        # Long file: chunk with overlap.
        for first_lineno, chunk_text in self._chunk_lines(lines):
            if should_skip_content(chunk_text):
                continue
            yield self._build_drawer(
                content=chunk_text,
                first_lineno=first_lineno,
                role=role,
                rel_path=rel_path,
                path=path,
                created_at=mtime,
            )

    # ----- Helpers --------------------------------------------------------

    def _relative_path(self, path: Path) -> str:
        """Render the path relative to ``source_root`` if set, else raw.

        Always uses forward slashes so thread_id strings are stable across
        Windows / Posix and across different working directories.
        """
        if self.source_root is not None:
            try:
                rel = path.resolve().relative_to(self.source_root.resolve())
                return rel.as_posix()
            except ValueError:
                # Path lives outside the declared root — fall back.
                pass
        return path.as_posix()

    def _infer_role(self, rel_path: str) -> str:
        """Pick ``wiki`` or ``memory`` from the relative path components.

        Default is ``wiki`` so unclassified markdown lands somewhere
        reasonable. ``memory`` wins if the path contains a ``memory``
        directory anywhere (Stefan's session logs, feedback files, etc.).
        """
        segments = rel_path.lower().split("/")
        if "memory" in segments:
            return "memory"
        if "wiki" in segments:
            return "wiki"
        return "wiki"

    def _chunk_lines(self, lines: list[str]) -> Iterator[tuple[int, str]]:
        """Yield ``(first_lineno, chunk_text)`` pairs.

        ``first_lineno`` is 1-based. Chunks step forward by
        ``chunk_size - chunk_overlap`` so consecutive chunks share
        ``chunk_overlap`` lines of context.
        """
        step = self.chunk_size - self.chunk_overlap
        n = len(lines)
        start = 0
        while start < n:
            end = min(start + self.chunk_size, n)
            chunk = "\n".join(lines[start:end])
            yield (start + 1, chunk)
            if end >= n:
                break
            start += step

    def _build_drawer(
        self,
        content: str,
        first_lineno: int,
        role: str,
        rel_path: str,
        path: Path,
        created_at: int,
    ) -> Drawer:
        """Assemble a Drawer from the chunked-or-whole markdown body."""
        content_hash = compute_content_hash(role, content)
        source_id = f"{rel_path}:{first_lineno}"
        return Drawer(
            source=self.name,
            source_id=source_id,
            role=role,
            content=content,
            created_at=created_at,
            content_hash=content_hash,
            source_path=str(path.resolve()),
            thread_id=rel_path,
            parent_uid=None,
            position_in_thread=first_lineno,
            metadata={"first_lineno": first_lineno},
        )
