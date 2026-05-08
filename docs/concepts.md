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
`{source}:{source_id}:{content_hash[:12]}`. The hash is SHA-256 over the
normalized content. The `drawer_uid` is the foreign-key target everywhere
else in the schema, so it survives `VACUUM` and FTS5 rebuilds without
breaking citations.

### Index

SQLite FTS5 (BM25) over every drawer. Fast.

The FTS5 virtual table uses `content='drawer_meta'` (external content
table) so storage isn't doubled. Tokenizer is `unicode61
remove_diacritics 2` — works for English, Latin-script European
languages, and a fair chunk of CJK without further config. For
multilingual semantic retrieval, see the `[multilingual]` extra.

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

The cross-encoder default is `cross-encoder/ms-marco-MiniLM-L-6-v2`
(English MS-MARCO trained). Multilingual variant ships in the
`[multilingual]` extra.

See [CLI](cli.md) for command-level usage and
[contracts.md](contracts.md) for the schema, MCP JSON shapes, and
plugin.json structure.
