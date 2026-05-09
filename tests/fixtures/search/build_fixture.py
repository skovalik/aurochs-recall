"""Build the deterministic search test fixture (`recall.db`).

Run from repo root:
    python -m tests.fixtures.search.build_fixture

The fixture is committed to the repo so CI and local tests use the exact
same database. Re-running this script regenerates it byte-for-byte.

Layout: 20 drawers across 3 sources (claude_code, claude_ai, markdown), each
with deterministic timestamps and content built around a small set of seed
terms (``acme``, ``lorem``, ``pricing``, ``recall``, ``sam``). This
keeps test queries small and predictable.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Allow `python tests/fixtures/search/build_fixture.py` from repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aurochs_recall.core.types import compute_content_hash, compute_drawer_uid  # noqa: E402


FIXTURE_PATH = Path(__file__).parent / "recall.db"
SCHEMA_PATH = REPO_ROOT / "aurochs_recall" / "core" / "migrations" / "0001_initial.sql"


# 20 deterministic drawers. (source, source_id, role, content, created_at,
#                            register, thread_id, position_in_thread)
DRAWERS = [
    # claude_code session — 6 messages around Acme Corp pricing
    ("claude_code", "session-aaaa-001", "human",
     "Lorem ipsum dolor sit amet. Stefan asked about Acme Corp pricing.",
     1704067200, None, "session-aaaa", 0),
    ("claude_code", "session-aaaa-002", "assistant",
     "The Acme Corp pricing breakdown shows that Bank Y uses a value-based model. Lorem ipsum.",
     1704067260, "technical", "session-aaaa", 1),
    ("claude_code", "session-aaaa-003", "human",
     "Can you recall the comparison with Sam Doe's pricing approach?",
     1704067320, None, "session-aaaa", 2),
    ("claude_code", "session-aaaa-004", "assistant",
     "Sam Doe's pricing is fundamentally different — he builds infra not branding.",
     1704067380, "technical", "session-aaaa", 3),
    ("claude_code", "session-aaaa-005", "human",
     "What about recall and search behaviour for the Acme Corp dashboard?",
     1704067440, None, "session-aaaa", 4),
    ("claude_code", "session-aaaa-006", "assistant",
     "Recall the Acme Corp dashboard supports both keyword search and BM25 ranking.",
     1704067500, "technical", "session-aaaa", 5),

    # claude_ai exported conversation — 5 messages, older
    ("claude_ai", "conv-bbbb-001", "human",
     "I told them pricing would be tied to outcomes, not deliverables.",
     1700000000, None, "conv-bbbb", 0),
    ("claude_ai", "conv-bbbb-002", "assistant",
     "That aligns with the Acme Corp pricing model — pure performance-based fees.",
     1700000100, "selling", "conv-bbbb", 1),
    ("claude_ai", "conv-bbbb-003", "human",
     "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
     1700000200, None, "conv-bbbb", 2),
    ("claude_ai", "conv-bbbb-004", "assistant",
     "Lorem ipsum is filler text traditionally used in design mockups.",
     1700000300, "teaching", "conv-bbbb", 3),
    ("claude_ai", "conv-bbbb-005", "human",
     "Sam Doe runs Acme AI — MCP infrastructure work.",
     1700000400, None, "conv-bbbb", 4),

    # markdown notes — 9 short entries
    ("markdown", "notes/pricing-2026.md", "wiki",
     "# Pricing notes 2026\n\nAcme Corp pricing strategy: outcome-tied retainers with milestones.",
     1710000000, None, "notes/pricing-2026.md", 0),
    ("markdown", "notes/recall-architecture.md", "wiki",
     "# Recall architecture\n\nFour layers: drawers, index, graph, access log. Lorem ipsum filler.",
     1710086400, None, "notes/recall-architecture.md", 0),
    ("markdown", "notes/sam-doe.md", "wiki",
     "# Sam Doe\n\nFractional CTO Robin. Springfield. MCP infra. Angel investor.",
     1710172800, None, "notes/sam-doe.md", 0),
    ("markdown", "notes/lorem-tests.md", "wiki",
     "Lorem ipsum dolor sit amet. Consectetur. Lorem in different context. Lorem.",
     1710259200, None, "notes/lorem-tests.md", 0),
    ("markdown", "notes/decisions.md", "wiki",
     "# Decisions\n\n- Use BM25 for T0\n- Hybrid search later\n- Recall.db on local disk only",
     1710345600, None, "notes/decisions.md", 0),
    ("markdown", "notes/personal.md", "memory",
     "Personal note: drink more water. Sleep is a deliverable too.",
     1710432000, "personal", "notes/personal.md", 0),
    ("markdown", "notes/voice.md", "wiki",
     "Voice register guide: selling, technical, teaching, personal, operational, playful_swagger.",
     1710518400, "teaching", "notes/voice.md", 0),
    ("markdown", "notes/stale.md", "wiki",
     "Stale entry. Has nothing to do with anything. Filler. Random text only.",
     1710604800, None, "notes/stale.md", 0),
    ("markdown", "notes/recall-cli.md", "wiki",
     "Recall CLI: init, index, search, status, errors, migrate, verify. Default mode bm25.",
     1710691200, None, "notes/recall-cli.md", 0),
]


def main() -> None:
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = sqlite3.connect(str(FIXTURE_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)
    conn.execute("INSERT INTO schema_version (version, applied_at, description, status) "
                 "VALUES (1, 0, 'baseline', 'applied')")

    for (source, source_id, role, content, created_at, register,
         thread_id, position) in DRAWERS:
        content_hash = compute_content_hash(role, content)
        drawer_uid = compute_drawer_uid(source, source_id, content_hash)
        cur = conn.execute(
            "INSERT INTO drawer_meta ("
            "  drawer_uid, source, source_id, source_path, role, register, "
            "  thread_id, parent_uid, position_in_thread, branch_count, "
            "  created_at, content_hash, risk_score, risk_score_version, "
            "  hash_input_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, 0, 1, 1)",
            (
                drawer_uid, source, source_id,
                f"/fixtures/{source}/{source_id}",
                role, register, thread_id, position, created_at, content_hash,
            ),
        )
        rowid = cur.lastrowid
        conn.execute(
            "INSERT INTO drawers_fts (rowid, content) VALUES (?, ?)",
            (rowid, content),
        )
    conn.execute(
        "INSERT INTO index_state (source, source_path, last_indexed_mtime, "
        "last_indexed_size, drawer_count) "
        "VALUES ('claude_code', '/fixtures/claude_code', 1704067500, 1024, 6),"
        "       ('claude_ai',   '/fixtures/claude_ai',   1700000400, 2048, 5),"
        "       ('markdown',    '/fixtures/markdown',    1710691200, 4096, 9)"
    )
    conn.commit()
    conn.close()
    size = FIXTURE_PATH.stat().st_size
    print(f"Wrote {FIXTURE_PATH} ({size} bytes, {len(DRAWERS)} drawers)")


if __name__ == "__main__":
    main()
