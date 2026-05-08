# External contracts

This page is the contract surface that other tools depend on. Anything
documented here is part of the public API and follows
[semver](https://semver.org/) at the major-version level.

## Outline

- **`sources.toml` schema** — see [sources.md](sources.md)
- **`seed-entities.toml`** — placeholder, populated during build
- **`seed-predicates.toml`** — placeholder, populated during build
- **`pii-rules.local` format** — see
  [`.githooks/pii-rules.example`](https://github.com/skovalik/aurochs-recall/blob/main/.githooks/pii-rules.example)
- **MCP tool JSON schemas** — placeholder; populated when the `[mcp]`
  extra is implemented
- **`plugin.json` (Claude Code plugin)** — May 2026 schema; placeholder
- **Hook stdin/stdout contract** — placeholder
- **Python API stability declaration** — placeholder

## Why this page exists

If you're integrating against aurochs-recall — building a custom
ingestor, wrapping the MCP server, or shipping a plugin that calls
`core.searcher` — you should pin against the contracts on this page.
Internal modules outside of this page are subject to change between
minor versions without notice.

## Stability tiers

- **Stable** — semver major break only. CLI flags marked stable, the
  on-disk SQLite schema (with migration coverage), `sources.toml`
  schema, MCP tool JSON shapes, the `core/searcher.py` public functions.
- **Experimental** — minor-version breakable with a CHANGELOG note.
  Cross-encoder model selection, the rerank score normalization, the
  `extract_pending` worktable shape.
- **Internal** — anything not on this page.

## TODO

This page is a placeholder; full schema dumps land during build.
