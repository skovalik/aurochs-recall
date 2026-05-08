"""Core dataclasses for aurochs-recall.

This is the canonical home for ``Drawer``, ``Hit``, ``Entity``, and
``Relationship``. The spine owns these — ingestors and retrievers import
from here.

All public types are frozen, slot-based, and stdlib-only. Drawers in
particular are IMMUTABLE: a drawer's content never changes once written;
if upstream content shifts, a new ``Drawer`` is created with a new
``drawer_uid``.

Stable identity rule (plan v5):

    drawer_uid = f"{source}:{source_id}:{content_hash[:12]}"

``content_hash`` is sha256 of ``role + RS + normalize_whitespace(content)``
where RS is the ASCII record-separator (0x1F). The ``hash_input_version``
column on ``drawer_meta`` tracks which normalization rule produced a
given hash so future bumps don't silently invalidate old uids.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Versioned constants
# ---------------------------------------------------------------------------

# Bumping any of these requires a drawer_uid migration tracked in the schema.
HASH_INPUT_VERSION: int = 1
RISK_SCORE_VERSION: int = 1

_RECORD_SEPARATOR: str = "\x1f"
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")


def normalize_whitespace(content: str) -> str:
    """Canonicalize whitespace for content_hash stability.

    Trims leading/trailing whitespace and collapses every internal whitespace
    run (spaces, tabs, newlines, NBSP) to a single ASCII space. This is the
    v1 normalization rule; bumping it requires a drawer_uid migration.
    """
    if not isinstance(content, str):
        raise TypeError(f"content must be str, got {type(content).__name__}")
    return _WHITESPACE_RUN.sub(" ", content).strip()


def compute_content_hash(role: str, content: str) -> str:
    """Compute the SHA-256 content_hash for a drawer.

    Hash input format::

        sha256(role || "\\x1f" || normalize_whitespace(content))

    Returns a 64-character lowercase hex digest.
    """
    if not isinstance(role, str):
        raise TypeError(f"role must be str, got {type(role).__name__}")
    payload = f"{role}{_RECORD_SEPARATOR}{normalize_whitespace(content)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_drawer_uid(source: str, source_id: str, content_hash: str) -> str:
    """Compute a stable drawer_uid from its identity triple.

    The first 12 hex chars of content_hash are sufficient to disambiguate
    drawers within a single (source, source_id) pair while keeping the uid
    short enough for git-short-SHA-style prefix matching at the CLI.
    """
    if not source:
        raise ValueError("source must be non-empty")
    if not source_id:
        raise ValueError("source_id must be non-empty")
    if not content_hash:
        raise ValueError("content_hash must be non-empty")
    if len(content_hash) < 12:
        raise ValueError("content_hash must be at least 12 chars (sha256 hex)")
    return f"{source}:{source_id}:{content_hash[:12]}"


# ---------------------------------------------------------------------------
# Drawer
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Drawer:
    """A unit of recall — verbatim text from a conversation, with thread
    metadata, provenance, and a stable content-derived identity.

    Drawers are IMMUTABLE. The content text is preserved verbatim — never
    paraphrased, never normalized for storage. ``content_hash`` is computed
    against the whitespace-normalized form for dedup, but the original
    ``content`` is what FTS5 indexes and what gets returned by ``get_drawer``.

    Fields map 1:1 onto ``drawer_meta`` columns; FTS5 stores the searchable
    text out-of-line keyed by rowid.

    Attributes
    ----------
    source:
        Which ingestor produced this drawer. One of:
        ``claude_code | claude_ai | chatgpt | markdown | capture``.
    source_id:
        Per-source identifier. Format depends on ingestor (see plan v4
        per-ingestor mapping table).
    role:
        ``human | assistant | wiki | memory | capture``.
    content:
        Verbatim text — not normalized, not trimmed, not paraphrased.
    created_at:
        Epoch seconds when the drawer was originally produced.
    content_hash:
        SHA-256 hex of ``f"{role}\\x1f{normalize_whitespace(content)}"``.
        Computed automatically if not supplied.
    source_path:
        Absolute path to the file the drawer came from, if any.
    register:
        Voice classification (selling | technical | teaching | personal |
        operational | playful_swagger | warm_authority | client_adaptive).
        Set later by the classifier; ingestors leave it ``None``.
    thread_id:
        Per-ingestor mapping (conversation/session UUID, file path, etc.).
    parent_uid:
        ``drawer_uid`` of the previous message in the same thread, or
        ``None`` for thread-start drawers.
    position_in_thread:
        Sequential index within the thread.
    branch_count:
        How many drawers point at this one as their parent. Set post-ingest.
    risk_score:
        0-100 multi-pass safety score. Default 0; populated by the scanner.
    risk_score_version:
        Which scoring algorithm produced ``risk_score``.
    hash_input_version:
        Which ``normalize_whitespace`` rule produced ``content_hash``.
    metadata:
        Per-ingestor scratch space — schema versions, message types, etc.
    """

    source: str
    source_id: str
    role: str
    content: str
    created_at: int
    content_hash: str = ""
    source_path: str | None = None
    register: str | None = None
    thread_id: str | None = None
    parent_uid: str | None = None
    position_in_thread: int | None = None
    branch_count: int = 0
    risk_score: int = 0
    risk_score_version: int = RISK_SCORE_VERSION
    hash_input_version: int = HASH_INPUT_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Frozen dataclasses require object.__setattr__ for late init.
        if not self.role:
            raise ValueError("role must be non-empty")
        if not self.source:
            raise ValueError("source must be non-empty")
        if not self.source_id:
            raise ValueError("source_id must be non-empty")
        if not 0 <= self.risk_score <= 100:
            raise ValueError(f"risk_score out of range: {self.risk_score}")

        # Lazy content_hash derivation — callers can pass a pre-computed
        # hash or let us compute it. Either way, validate it's non-empty.
        if not self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                compute_content_hash(self.role, self.content),
            )

    @property
    def drawer_uid(self) -> str:
        """Stable composite identity used as FK target across the schema.

        Format: ``{source}:{source_id}:{content_hash[:12]}``.
        """
        return compute_drawer_uid(self.source, self.source_id, self.content_hash)


# ---------------------------------------------------------------------------
# Search hits
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Hit:
    """A search result — a citation back into the drawer corpus.

    ``score`` is mode-dependent (BM25 negative-log-likelihood for FTS5, RRF
    fused rank for hybrid, cross-encoder logit for semantic). Consumers
    should treat it as opaque-but-sortable per query.

    ``rank`` is the 1-based position the retriever assigned (1 = best).
    """

    drawer_uid: str
    score: float
    snippet: str
    source: str
    created_at: int
    rank: int = 0


# ---------------------------------------------------------------------------
# Knowledge graph
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Entity:
    """A node in the knowledge graph — a person, project, concept, etc.

    ``id`` is the SQLite-assigned primary key. ``source`` is one of
    ``seed | llm | manual``; the spine ships seed entities only.
    """

    id: int
    name: str
    type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    first_seen: int | None = None
    last_seen: int | None = None
    source: str = "seed"


@dataclass(frozen=True, slots=True)
class Relationship:
    """An edge in the knowledge graph, optionally cited by drawer_uid.

    ``valid_to`` is ``None`` while the relationship is currently valid;
    flipping to a timestamp is how the bitemporal model retires edges
    without deleting them. ``drawer_uid`` provides the citation when
    the edge was extracted from a specific drawer.
    """

    id: int
    subject_id: int
    predicate: str
    object_id: int
    valid_from: int | None = None
    valid_to: int | None = None
    drawer_uid: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
