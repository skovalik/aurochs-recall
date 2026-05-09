# bench/datasets/

Bundled, synthetic, deterministic. Every byte in this directory is
regenerable from a seed via `bench.scripts.generate_synthetic` — there is
no hand-curated content here.

## Files

| File                                 | Records | What                                                       |
| ------------------------------------ | ------- | ---------------------------------------------------------- |
| `synthetic_chat.jsonl`               | ~600    | 100 synthetic Claude-Code-style conversation threads, 4-8 messages each. One drawer per line. |
| `synthetic_chat_queries.jsonl`       | 50      | Query / expected-drawer-uid pairs targeting one drawer each. |

## Generation methodology

Each thread carries a unique anchor token (e.g. `cipher-validator-rev-122`)
embedded in the first human message. The anchor never appears in any other
thread, so a query containing the anchor has exactly one correct answer.
Queries are built two ways: the anchor alone (~50%) and the anchor paired
with a meaningful word from the target drawer's body (~50%). Both should
recall@1 cleanly because the anchor is the discriminator.

The generator is deterministic. Same seed in (default `17`) always produces
byte-identical JSONL files — sorted keys, UTF-8, LF endings. To regenerate:

```bash
python -m bench.scripts.generate_synthetic
```

To verify nothing has been hand-edited, regenerate and diff:

```bash
python -m bench.scripts.generate_synthetic --out-dir /tmp/regen
diff bench/datasets/synthetic_chat.jsonl /tmp/regen/synthetic_chat.jsonl
diff bench/datasets/synthetic_chat_queries.jsonl /tmp/regen/synthetic_chat_queries.jsonl
```

## What this dataset measures

Indexer-and-retriever correctness on **unambiguous, single-token-discriminator**
retrieval. It does NOT exercise:

- Semantic ambiguity (no near-duplicate threads).
- Multi-document evidence (one query → one expected drawer).
- Long-tail vocabulary (templates are deliberately bland to avoid ToS issues).
- Cross-language retrieval (English-only).

Use this dataset to detect **regressions** (a working build should hit
~100% precision@1) and as a **smoke check** in CI. For meaningful relative
ranking of retrieval strategies, run against a real corpus or a public
retrieval benchmark.

## Disclosures

- No PII. No real conversations. No scraped content.
- Templates are intentionally generic engineering prose.
- Anchor tokens are random word combinations; they should not match any
  real product, person, or vendor.
- The corpus is public-distribution-safe and is shipped with the package.

## Ground truth notes

The `expected_drawer_uid` field is computed via
`compute_drawer_uid(source, source_id, content_hash[:12])`. If the
content_hash algorithm changes (`HASH_INPUT_VERSION` bumps in
`core/types.py`), the queries file must be regenerated.
