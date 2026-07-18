# aurochs-recall

Memory architecture for AI conversations. Verbatim drawers in local SQLite, an FTS5 BM25 index, an entity knowledge graph, an access log. Public MIT product: three surfaces (the `recall` CLI, an MCP server, a Claude Code plugin) projected from one `core/` API. The CLI is the source of truth; the MCP server and the plugin both call into the same `core/`.

Sibling repo under the lean-config pattern: own `.claude`, minimal plugin surface, single-author commits, no inheritance from the mega-workspace.

## Rules

1. Plan before code. No product code until Stefan approves a plan in-session. Plans live in `docs/plans/`, dated `YYYY-MM-DD-slug.md`.
2. Verify at runtime, never assume. A claim about behavior is a hypothesis until you run it and observe the result. This indexes a real corpus, so a passing test is not proof the live database is intact: `recall status` and `recall verify` are the checks.
3. Match existing patterns. Read `core/` before adding to it. New retrievers implement `retriever/_base.py`, new ingest sources implement `ingest/_base.py`, and schema changes go through `core/migrations/` as a numbered SQL file, never an ad-hoc ALTER.
4. Public from commit 1. Everything committed here ships to github.com/skovalik/aurochs-recall under MIT. No client names, no personal corpus content, no local paths, no secrets. All test fixtures are synthetic.
5. The `.githooks/` pre-commit hooks block PII and secret patterns. Do not bypass them with `--no-verify`. If a hook fires, fix the content.
6. Single-author commits. Never add a Claude co-author trailer.
7. No em-dashes or en-dashes in any copy, docs, code comments, or CLI output in this repo.

## What lives here

- `aurochs_recall/core/`: the engine. db, schema, migrations, index, search, `retriever/`, `graph/`, `ingest/`, BYOK extraction, recovery, validation.
- `aurochs_recall/cli/`: the `recall` console entry point (`aurochs_recall.cli.main:main`).
- `aurochs_recall/mcp/server.py`: the MCP server (`python -m aurochs_recall.mcp.server`).
- `commands/` and `.claude-plugin/`: the plugin surface. Four slash commands plus the manifest that auto-wires the MCP server.
- `docs/`: the mkdocs-material site. `bench/`: LongMemEval reproduction plus a personal-corpus eval template.
- `tests/`: unit and integration, synthetic fixtures only.

## State

Runtime state lives outside the repo, resolved through platformdirs in `core/sources_config.py`: `recall.db` under the user data dir, `sources.toml` under the user config dir. Neither is version-controlled, and neither is safe to assume present. Hard dependency on the sibling package `aurochs-core`.

Machine-specific setup notes for a given working copy belong in `CLAUDE.local.md`, which is untracked and never published.
