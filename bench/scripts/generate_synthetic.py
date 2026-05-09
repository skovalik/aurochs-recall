"""Deterministic synthetic dataset generator for the bench harness.

The generator emits two JSONL files:

* ``synthetic_chat.jsonl`` — 100 synthetic Claude-Code-style conversations.
  Each line is one drawer (``source_id`` + ``role`` + ``content`` + ``thread_id``
  + ``position_in_thread`` + ``created_at``). Threads are 4-8 messages long.
* ``synthetic_chat_queries.jsonl`` — 50 query/answer pairs. Each line carries
  a ``query`` string plus the ``expected_drawer_uid`` of the drawer the query
  uniquely identifies. Used by ``bench.run`` for precision/recall@k.

Determinism: the generator is seeded so the same seed always yields the same
bytes. We use ``random.Random(seed)`` (no global state) and write JSONL with
``sort_keys=True`` + LF line endings so the file is byte-stable across runs
and across Python versions.

Quality of the corpus:

The drawers contain "anchor" tokens that uniquely identify a single thread
(e.g. ``mehrwerk-pricing-2026-04-21``). The query/answer set is built so each
query contains one anchor and the expected drawer is the one carrying that
anchor. This gives us ground-truth labels without any human annotation. It's
not a substitute for a hand-labeled benchmark — it tests indexer-and-retriever
correctness, not semantic ambiguity handling.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow ``python -m bench.scripts.generate_synthetic`` from repo root by
# ensuring the package root is importable.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aurochs_recall.core.types import compute_content_hash, compute_drawer_uid  # noqa: E402

# ---------------------------------------------------------------------------
# Generator configuration
# ---------------------------------------------------------------------------

DEFAULT_SEED: int = 17  # arbitrary; doesn't matter what the value is
DEFAULT_THREAD_COUNT: int = 100
DEFAULT_QUERY_COUNT: int = 50
DEFAULT_BASE_TIMESTAMP: int = 1_700_000_000  # 2023-11-14 UTC
DEFAULT_TIMESTAMP_STEP: int = 60  # one minute between drawers in a thread

# Synthetic anchor tokens. Each is unique and easy to grep for; the query
# generator picks one per thread so retrieval has unambiguous ground truth.
_ANCHOR_PREFIXES: tuple[str, ...] = (
    "atlas",
    "borealis",
    "cipher",
    "delta",
    "ember",
    "flint",
    "gilded",
    "horizon",
    "ionic",
    "junction",
    "kestrel",
    "lattice",
    "monolith",
    "nimbus",
    "obsidian",
    "prism",
    "quasar",
    "rivet",
    "sentinel",
    "tundra",
)

# Topic sentence templates. Each %s is replaced by an anchor token. We
# deliberately avoid PII / real names — the bench is shipped with the repo
# so its corpus has to be safe for public distribution.
_TOPIC_TEMPLATES: tuple[str, ...] = (
    "How do we wire up the %s pipeline so it survives a partial restart?",
    "Could you walk me through the %s migration plan one more time?",
    "I keep hitting a deadlock on %s — what's the recommended workaround?",
    "Can the %s validator be configured to accept relative paths too?",
    "Why does the %s scheduler skip the first run after a cold start?",
    "What's the cheapest way to backfill %s without touching the live index?",
    "Is there a published benchmark for %s on commodity hardware?",
    "Where is the source of truth for the %s schema — repo or docs site?",
    "Can the %s retriever swap to BM25 mode if the cross-encoder is unavailable?",
    "How do I add a new backend to %s without forking the whole package?",
)

# Reply templates do NOT contain ``%s`` — the assistant's reply uses
# pronouns ("it", "the system") so only the first human turn carries
# the anchor. This keeps the ground-truth label single-relevant-doc:
# the anchor appears in exactly one drawer per thread.
_REPLY_TEMPLATES: tuple[str, ...] = (
    "Yes — the system reads from a watermark table, so a partial run resumes from the last good record. Hand it the same input twice and it dedupes.",
    "Sure. Step one is to run the migration in dry-run mode; step two is to apply with the lock acquired; step three is to verify the version row landed. We document the sequence in docs/migrations.md.",
    "The deadlock happens when two writers race on the lockfile. The standard workaround is to set the busy_timeout to 30s and retry once on SQLITE_BUSY.",
    "Out of the box, the validator only accepts absolute paths. There's a feature flag in the config (validator.allow_relative=true) if you want the relaxed behaviour.",
    "The scheduler skips its first run after a cold start because it's waiting on a leadership election. Start it once, stop it, start it again — the second boot will run normally.",
    "The cheapest backfill is to write to a side-table and swap atomically. Touching the live index doubles the I/O for hours and kills query latency in the meantime.",
    "There's no canonical published benchmark; we ship ours in bench/. Numbers vary by SSD class — the harness reports p50/p95/p99 so you can compare apples to apples.",
    "The schema lives in the repo at core/migrations/0001_initial.sql. The docs site is generated from it; treat the SQL file as canonical.",
    "Yes — there's a fallback chain. If the cross-encoder model isn't on disk, it degrades to BM25 only and logs a warning at startup.",
    "Adding a new backend is a Protocol implementation. See core/ingest/_base.py — implement can_handle and extract, register in the dispatcher, and the rest is wired up automatically.",
)

_FOLLOWUP_TEMPLATES: tuple[str, ...] = (
    "Got it. One more thing — does the flow log every retry to ingest_errors?",
    "Makes sense. Is there a CI matrix that exercises this on Windows specifically?",
    "Thanks. So the status output includes wal_size_pages now?",
    "Cool. Can I forget a single drawer with --dry-run, or only batch?",
    "Right. If I bump the normalization version, does it require a re-index?",
    "Understood. Is the lockfile released cleanly on SIGTERM?",
    "Thanks for clarifying. Does it run the multi-pass safety scanner on every drawer?",
    "Perfect. What's the minimum SQLite version the schema requires?",
    "Sounds good. Are there backup commands tied to this, or do I roll my own?",
    "Got it. If I want to ship this as a plugin, what's the entry-point shape?",
)

_FOLLOWUP_REPLIES: tuple[str, ...] = (
    "Yes — every retry on the flow lands in ingest_errors with retry_count incremented and the latest reason. The CLI's `recall errors` reads from there.",
    "Yes. The CI matrix runs ubuntu/windows/macos x {system Python sqlite, pysqlite3-binary fallback} so we catch tokenizer drift between bundled sqlite versions.",
    "That's right — the status row was added in v5. It surfaces wal_size_pages so you can see when wal_autocheckpoint is falling behind under MCP burst.",
    "Single-uid forget supports --dry-run as of v5. The fast path (no flag) still hides immediately; --dry-run preserves symmetry with the batch path.",
    "Yes. Bumping the hash_input_version requires a drawer_uid migration because the content_hash changes. The version column on drawer_meta lets you detect mismatches.",
    "On SIGTERM the lockfile is released via the cleanup handler. Stale-PID detection covers the case where the parent died without unlocking.",
    "Yes — every drawer goes through the multi-pass scanner at ingest. Risk score, version, and per-pass evidence are all stored.",
    "The schema requires SQLite 3.35+ for STRICT tables and modern FTS5 features. We pin a fallback (pysqlite3-binary) for systems with older bundled sqlite.",
    "There are first-class backup commands: backup, restore, verify. They use the SQLite Online Backup API — atomic, hot, and crash-safe.",
    "It ships as a Claude Code plugin. The entry point is the recall CLI binary; the plugin manifest declares it as the only command and binds the keyword.",
)


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DrawerRecord:
    """A single synthetic drawer record, ready to serialize as JSONL.

    Mirrors the columns the bench harness needs (no FTS5 mirror, no
    metadata blob — those are produced by the indexer at load time).
    """

    source: str
    source_id: str
    role: str
    content: str
    created_at: int
    thread_id: str
    position_in_thread: int

    def as_jsonl_dict(self) -> dict[str, object]:
        """Render the canonical JSONL dict shape, deterministic key order."""
        return {
            "content": self.content,
            "created_at": self.created_at,
            "position_in_thread": self.position_in_thread,
            "role": self.role,
            "source": self.source,
            "source_id": self.source_id,
            "thread_id": self.thread_id,
        }


@dataclass(frozen=True, slots=True)
class _QueryRecord:
    """A query / expected-drawer-uid pair for the retrieval bench."""

    query: str
    expected_drawer_uid: str
    anchor: str  # the unique token that ties the query to its answer

    def as_jsonl_dict(self) -> dict[str, object]:
        return {
            "anchor": self.anchor,
            "expected_drawer_uid": self.expected_drawer_uid,
            "query": self.query,
        }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _build_anchor(rng: random.Random, used: set[str]) -> str:
    """Build a unique anchor token, e.g. ``borealis-pipeline-rev-37``.

    The anchor is short, lowercase, hyphen-separated, and easy to grep
    for. Uniqueness is enforced via the ``used`` set passed in by the
    caller — the generator retries on collision.
    """
    while True:
        prefix = rng.choice(_ANCHOR_PREFIXES)
        modifier = rng.choice(("pipeline", "schema", "scheduler", "retriever", "validator"))
        rev = rng.randint(1, 199)
        anchor = f"{prefix}-{modifier}-rev-{rev}"
        if anchor not in used:
            used.add(anchor)
            return anchor


def _generate_thread(
    rng: random.Random,
    thread_index: int,
    anchor: str,
    base_timestamp: int,
) -> list[_DrawerRecord]:
    """Generate one synthetic conversation thread.

    Threads are 4-8 messages long, alternating human/assistant. The first
    human turn embeds the anchor; subsequent turns reference it implicitly
    so the topic-anchor association is unique to the first drawer.
    """
    length = rng.randint(4, 8)
    thread_id = f"synthetic-thread-{thread_index:04d}"
    drawers: list[_DrawerRecord] = []

    # First human turn — carries the anchor verbatim. This is the drawer
    # the query/answer generator targets.
    first_template = rng.choice(_TOPIC_TEMPLATES)
    drawers.append(
        _DrawerRecord(
            source="synthetic_chat",
            source_id=f"{thread_id}-msg-000",
            role="human",
            content=first_template % anchor,
            created_at=base_timestamp,
            thread_id=thread_id,
            position_in_thread=0,
        )
    )

    # Pair the human turn with an assistant reply that does NOT carry the
    # anchor — uses pronouns instead so the anchor stays unique to drawer 0.
    reply_template = rng.choice(_REPLY_TEMPLATES)
    drawers.append(
        _DrawerRecord(
            source="synthetic_chat",
            source_id=f"{thread_id}-msg-001",
            role="assistant",
            content=reply_template,
            created_at=base_timestamp + DEFAULT_TIMESTAMP_STEP,
            thread_id=thread_id,
            position_in_thread=1,
        )
    )

    # Remaining turns — alternating, follow-up template + reply. Neither
    # carries the anchor so retrieval-by-anchor maps to drawer 0 only.
    for i in range(2, length):
        is_human = (i % 2) == 0
        if is_human:
            template = rng.choice(_FOLLOWUP_TEMPLATES)
            role = "human"
        else:
            template = rng.choice(_FOLLOWUP_REPLIES)
            role = "assistant"
        drawers.append(
            _DrawerRecord(
                source="synthetic_chat",
                source_id=f"{thread_id}-msg-{i:03d}",
                role=role,
                content=template,
                created_at=base_timestamp + (i * DEFAULT_TIMESTAMP_STEP),
                thread_id=thread_id,
                position_in_thread=i,
            )
        )

    return drawers


def _build_query(rng: random.Random, anchor: str, target_content: str) -> str:
    """Build a search query that contains the unique anchor.

    Two flavors of query, balanced ~50/50:

    * **Anchor-alone** (single-token retrieval) — query is just the
      anchor. Tests that hyphen-tokenized anchors recall their drawer.
    * **Adjacent phrase** — anchor + the word that follows it in the
      target drawer. Phrase-mode FTS5 needs *adjacent* tokens, not just
      a co-occurrence. Picking the next word from the target content
      keeps phrase queries answerable.
    """
    if rng.random() < 0.5:
        return anchor

    # Find the anchor's position in the target and grab the next word.
    # The anchor is guaranteed to appear (we just generated it).
    words = target_content.split()
    for i, word in enumerate(words):
        # Strip trailing punctuation when matching but keep the original
        # for the phrase query so casing / question marks don't drift.
        cleaned = word.strip(".,?:!")
        if anchor in cleaned and i + 1 < len(words):
            next_word = words[i + 1].strip(".,?:!")
            if next_word and len(next_word) >= 3:
                return f"{anchor} {next_word}"
            break
    return anchor


def generate_dataset(
    *,
    seed: int = DEFAULT_SEED,
    thread_count: int = DEFAULT_THREAD_COUNT,
    query_count: int = DEFAULT_QUERY_COUNT,
    base_timestamp: int = DEFAULT_BASE_TIMESTAMP,
) -> tuple[list[_DrawerRecord], list[_QueryRecord]]:
    """Generate the full synthetic dataset (drawers + queries).

    Returns ``(drawer_records, query_records)``. Both lists are
    deterministic given ``seed``: same seed in, same bytes out.
    """
    if thread_count <= 0:
        raise ValueError("thread_count must be positive")
    if query_count <= 0:
        raise ValueError("query_count must be positive")
    if query_count > thread_count:
        raise ValueError(
            f"query_count ({query_count}) cannot exceed thread_count "
            f"({thread_count}); each query targets a distinct thread"
        )

    rng = random.Random(seed)
    used_anchors: set[str] = set()

    drawer_records: list[_DrawerRecord] = []
    thread_anchors: list[tuple[str, _DrawerRecord]] = []
    for thread_idx in range(thread_count):
        anchor = _build_anchor(rng, used_anchors)
        thread_base = base_timestamp + (thread_idx * 86_400)  # 1 day apart
        thread_drawers = _generate_thread(rng, thread_idx, anchor, thread_base)
        drawer_records.extend(thread_drawers)
        # The first drawer (index 0) is the canonical answer for queries
        # carrying this anchor.
        thread_anchors.append((anchor, thread_drawers[0]))

    # Sample query_count threads (without replacement) to build queries for.
    chosen = rng.sample(thread_anchors, query_count)
    query_records: list[_QueryRecord] = []
    for anchor, target_drawer in chosen:
        target_uid = compute_drawer_uid(
            target_drawer.source,
            target_drawer.source_id,
            compute_content_hash(target_drawer.role, target_drawer.content),
        )
        query_records.append(
            _QueryRecord(
                query=_build_query(rng, anchor, target_drawer.content),
                expected_drawer_uid=target_uid,
                anchor=anchor,
            )
        )

    return drawer_records, query_records


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    """Write ``records`` to ``path`` as JSON Lines, deterministic order.

    Sorted keys + UTF-8 + LF line endings so the same input always yields
    byte-identical output. The bench README documents this so users can
    diff regenerated files against the committed copy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline='' so the LF we write isn't translated to CRLF on Windows.
    with path.open("w", encoding="utf-8", newline="") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            fh.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: generate the bundled synthetic dataset.

    Default behavior writes ``bench/datasets/synthetic_chat.jsonl`` and
    ``bench/datasets/synthetic_chat_queries.jsonl``. Override with
    ``--out-dir`` to target a different location (useful for tests).
    """
    parser = argparse.ArgumentParser(
        description="Generate the synthetic-chat bench dataset."
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="RNG seed (default: %(default)s)"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREAD_COUNT,
        help="Number of conversation threads (default: %(default)s)",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=DEFAULT_QUERY_COUNT,
        help="Number of query/answer pairs (default: %(default)s)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "datasets",
        help="Directory to write the JSONL files into (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    drawer_records, query_records = generate_dataset(
        seed=args.seed,
        thread_count=args.threads,
        query_count=args.queries,
    )

    out_dir = Path(args.out_dir)
    drawers_path = out_dir / "synthetic_chat.jsonl"
    queries_path = out_dir / "synthetic_chat_queries.jsonl"

    write_jsonl(drawers_path, [d.as_jsonl_dict() for d in drawer_records])
    write_jsonl(queries_path, [q.as_jsonl_dict() for q in query_records])

    print(
        f"Wrote {len(drawer_records)} drawer records to {drawers_path}\n"
        f"Wrote {len(query_records)} query records to {queries_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
