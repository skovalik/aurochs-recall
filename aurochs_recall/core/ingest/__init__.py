"""Ingestor implementations for aurochs-recall.

Each ingestor produces an iterator of ``Drawer`` objects from one source
type. Per plan v5, T0 ships three ingestors:

* ``claude_code`` — Claude Code session jsonl (flat + nested layouts)
* ``claude_ai`` — Claude.ai web export (``conversations.json``)
* ``markdown`` — recursive walk of a directory of ``.md`` files

Skipped for T0 (deferred to later patches): ``chatgpt``, ``capture``.
"""

from __future__ import annotations

from ._base import IngestError, Ingestor
from .claude_ai import ClaudeAiIngestor
from .claude_code import ClaudeCodeIngestor
from .markdown import MarkdownIngestor

__all__ = [
    "ClaudeAiIngestor",
    "ClaudeCodeIngestor",
    "Ingestor",
    "IngestError",
    "MarkdownIngestor",
]


def get_default_ingestors() -> list[Ingestor]:
    """Return one instance of each T0 ingestor.

    Order matters for ``can_handle()`` resolution: the first matching
    ingestor wins. Markdown is last because its ``.md`` glob is broad
    enough to catch anything else's stray files.
    """
    return [
        ClaudeCodeIngestor(),
        ClaudeAiIngestor(),
        MarkdownIngestor(),
    ]
