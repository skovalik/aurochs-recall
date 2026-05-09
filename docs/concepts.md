# Concepts

aurochs-recall is structured as **four memory layers** that mirror how the
brain handles memory.

| Brain                  | aurochs-recall                       | Role                                         |
| ---------------------- | ------------------------------------ | -------------------------------------------- |
| Long-term storage      | `drawer_meta` + `drawers_fts`        | Verbatim text, immutable, the unit of recall |
| Short-term encoding    | Indexer pipeline                     | Takes new drawers and places them            |
| Semantic memory        | Entities + relationships + taxonomy  | Abstractions extracted from episodes         |
| Episodic memory        | `thread_id`, `parent_uid`, `position_in_thread` | Each drawer bound to a conversation |
| Meta-memory            | `access_log`                         | What you've recalled, when, how              |
| Reconsolidation        | Versioned extraction runs            | The graph evolves; the drawers don't         |

## The four user-visible layers

### Drawers

Verbatim text. **Immutable.** A drawer is the unit of recall.

A drawer has a stable identity (`drawer_uid`) derived from
`{source}:{source_id}:{content_hash[:12]}`. The hash is SHA-256 over
normalized content. The `drawer_uid` is the foreign-key target
everywhere else in the schema, so it survives `VACUUM` and FTS5 rebuilds
without breaking citations.

#### The drawer_uid stability contract

The `drawer_uid` is the single piece of identity recall promises to keep
stable across the lifetime of a drawer. Three discipline points hold the
contract:

1. **Content hashing is normalization-aware.** The `content_hash` is
   computed over a deterministic normalization of the drawer text — line
   endings collapsed, trailing whitespace trimmed, BOM stripped, etc.
   Two ingestors handed the same logical content always produce the
   same hash; round-tripping a drawer through `recall backup` /
   `recall restore` does not change the uid.
2. **Source ids are stable.** The `source_id` is anchored to whatever
   the source format makes durable: a session id for claude_code, a
   conversation id for claude.ai, a file path + content stamp for
   markdown. If the source format itself reissues ids (rare), the
   ingestor handles it as a one-time migration so existing drawer_uids
   keep resolving.
3. **`hash_input_version` tracks the normalization rule itself.** If we
   ever need to change *how* `normalize_whitespace()` works — say a
   future Unicode normalization fix — the `hash_input_version` column
   in `drawer_meta` records which rule was in force when the hash was
   computed. Migrations bump the version explicitly and rehash existing
   drawers under controlled conditions; they don't silently re-derive
   uids and break every citation across the database. This is the same
   versioning discipline already applied to `risk_score_version`.

The end result: a citation written against `drawer_uid` today will keep
resolving to the same verbatim drawer tomorrow, or the migration will
fail loudly and explicitly rather than silently mismapping.

### Index

SQLite FTS5 (BM25) over every drawer. Fast.

The FTS5 virtual table holds searchable content joined to drawer_meta
via `rowid`. Tokenizer is `unicode61 remove_diacritics 2` — works for
English, Latin-script European languages, and a fair chunk of CJK
without further config. For multilingual semantic retrieval, see the
`[multilingual]` extra.

#### Cross-encoder reranking with BM25 fallback

The default search mode is `hybrid`: FTS5 BM25 produces an over-fetched
candidate set, dense embeddings (with the `[embeddings]` extra)
re-score by semantic similarity, and a cross-encoder rerank stage
makes the final ordering call. The default cross-encoder is pinned to
`cross-encoder/ms-marco-MiniLM-L-6-v2` (English MS-MARCO trained); a
multilingual MiniLM variant ships in the `[multilingual]` extra.

If the cross-encoder isn't installed, hybrid mode degrades **gracefully**
to BM25 + embeddings ordering, and pure-BM25 mode is always available
via `--mode bm25`. The cross-encoder is an enhancement, not a
dependency: a recall query never fails because a model isn't loaded,
and BM25 alone is the floor everyone gets.

### Graph

Entities, relationships, citations. **Append-and-amend.**

The graph evolves as you re-extract — a process the schema calls
"reconsolidation," following the brain analogy. New extraction runs
either confirm existing relationships, add new ones, or amend (mark
old ones as superseded). The drawers themselves are never rewritten.

### Access log

What you've recalled, when, how. **Meta-memory.**

Every retrieval logs the query, the result drawers, the rerank score,
and the eventual user signal (clicked / quoted / ignored). Over time
this becomes a feedback channel: reranker calibration, query-rewriting
hints, "recently relevant" boosting.

## What flows through

```
ingestor -> drawer_meta + drawers_fts -> linker -> entities + relationships
                                       \-> access_log <- searcher <- user query
```

Ingestors normalize source-specific shapes (claude_code jsonl, claude.ai
export, chatgpt export, markdown vault) into the common drawer schema.
The linker uses a seed-list first; LLM extraction is opt-in via
`recall extract` and crash-safe via `extract_pending`.

Searchers run in three modes:

- `bm25` — pure FTS5
- `hybrid` — FTS5 + dense embeddings + cross-encoder rerank (default)
- `semantic` — dense-only (rare; mostly for ablation)

## BYOK extraction

The graph layer ships with a seed-list linker that runs without any
model — that's enough for a useful entity graph on day one. LLM
extraction is opt-in, **BYOK** (Bring Your Own Key), and routed
through whichever provider you configure:

- `ANTHROPIC_API_KEY` — Anthropic Claude (default model:
  `claude-haiku-4.5`)
- `OPENAI_API_KEY` — OpenAI (default model: `gpt-4-mini`)
- `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` — point either provider at
  a self-hosted vLLM, an Ollama OpenAI-compatible endpoint, or a
  Cloudflare AI Gateway. Recall doesn't care; the SDK does the routing.

See [BYOK](byok.md) for the full provider-routing matrix and
self-hosted recipes.

See [CLI](cli.md) for command-level usage and
[contracts.md](contracts.md) for the schema, MCP JSON shapes, and
plugin.json structure.
