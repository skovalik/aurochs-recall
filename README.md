# aurochs-recall

**Memory architecture for your AI conversations.**

Drawers preserve the verbatim text. The index makes it instantly findable. The knowledge graph remembers what's connected to what. Local SQLite. No paraphrasing. Citations everywhere.

*MIT · v0.1 · public from commit 1 · Stefan Kovalik <stefan@aurochs.agency>*

**Verify, back up, restore.** Multi-pass safety scanning · Versioned extractions · Stable citations across DB ops.

---

I was looking for a memory layer for my AI conversations that didn't paraphrase. The category had real options and a few hyped ones that didn't hold up to a closer look. What I found was that I'd already been taping pieces of this together myself across separate skills and notes — verbatim storage, FTS over a SQLite index, register classification, an entity graph for who-said-what-about-whom. This is the proper assembly of those pieces into one thing.

I built the bench because the category had hyped options that didn't hold up. Proof should be testable on your own machine.

---

**Privacy:** aurochs-recall does not transmit your data anywhere. There is no telemetry, no analytics, no crash reporting. The only network traffic is opt-in BYOK extraction calls to providers you explicitly configure. The recall.db file lives on your disk and you can read it with the standard SQLite CLI.

## Use as a Claude Code plugin

```bash
/plugin install aurochs-recall
```

Then in any Claude Code session:

- `/aurochs-recall <args>` — full CLI surface
- `/recall <query>` — quick search (shortcut)
- `/recall-status` — DB stats
- `/recall-forget <prefix>` — hide a drawer

The plugin auto-wires the recall MCP server, so Claude can also invoke recall tools natively.

## 30-second quickstart

```bash
pip install aurochs-recall
recall init                          # discovers your sources, writes starter config
recall "your first query"
```

## The four memory layers

- **Drawers** — verbatim text, immutable, the unit of recall.
- **Index** — sqlite FTS5 BM25 over every drawer. Fast.
- **Graph** — entities, relationships, citations. Append-and-amend.
- **Access log** — what you've recalled, when, how. Meta-memory.

## What this isn't

- It doesn't summarize your conversations.
- It doesn't paraphrase what you said.
- It doesn't decide what's "important" and forget the rest.
- It doesn't auto-cluster, auto-tag, or auto-anything you didn't ask for.
- It doesn't claim to "understand" your memory. It indexes it. There's a difference.
- It doesn't run in the cloud. The database file lives on your disk and you can read it with the standard SQLite CLI.
- It doesn't promise you a benchmark number. The bench is published with full methodology so you can run it yourself.

---

## Install

```bash
pip install aurochs-recall              # core: SQLite FTS5 + drawers + KG with seed-list linker
pip install "aurochs-recall[all]"       # everything below at once
```

Optional extras (install only what you need):

| Extra            | What it adds                                                            |
| ---------------- | ----------------------------------------------------------------------- |
| `[chroma]`       | ChromaDB vector store for semantic / hybrid retrieval                   |
| `[embeddings]`   | sentence-transformers (English MiniLM by default)                       |
| `[graph]`        | Kùzu graph database for fast multi-hop traversals                       |
| `[rerank-llm]`   | LLM-as-reranker (BYOK Anthropic / OpenAI)                               |
| `[multilingual]` | BGE-M3 + multilingual MiniLM cross-encoder                              |
| `[backup]`       | zstandard for compressed `recall backup`                                |
| `[dev]`          | pytest, ruff, mypy                                                      |
| `[docs]`         | mkdocs-material + the docs build chain                                  |

## Usage

```bash
recall init                              # discover sources, write sources.toml + run first ingest
recall "deadlock in pgbouncer"           # default mode: hybrid (BM25 + cross-encoder rerank)
recall "client onboarding playbook" --mode bm25
recall "voice deltas from last week" --since 7d
recall status                            # row counts, WAL size, lockfile state, schema version
recall verify                            # checksums + FK integrity + FTS rebuild check
recall backup ~/recall-backups/          # full snapshot incl. taxonomy_audit + access_log
recall restore ~/recall-backups/2026-05-07.db.zst
recall forget abc123de --dry-run         # preview hide; --apply to commit
recall extract --resume                  # crash-safe BYOK extraction restart
```

The CLI is the source of truth. The MCP server and the Claude Code plugin both call into the same underlying `core/` API.

## Configuration

`recall init` writes `~/.config/aurochs-recall/sources.toml` (Linux/macOS) or
`%APPDATA%\aurochs-recall\sources.toml` (Windows). Edit that file to add or
remove sources. Schema is documented in [`docs/sources.md`](docs/sources.md).

## Documentation

Full docs live under [`docs/`](docs/):

- [Concepts](docs/concepts.md) — the four memory layers
- [CLI reference](docs/cli.md)
- [Sources schema](docs/sources.md)
- [BYOK](docs/byok.md) — Anthropic / OpenAI / vLLM / Cloudflare AI Gateway
- [Privacy posture](docs/privacy.md)
- [Concurrency model](docs/concurrency.md)
- [Backup & restore](docs/backup-restore.md)
- [Migrations](docs/migrations.md)
- [Failure modes](docs/failure-modes.md)
- [External contracts](docs/contracts.md) — MCP schema, plugin.json, hook stdin/stdout
- [Comparison](docs/comparison.md) — where this sits in the memory-tooling category
- [Empirical receipts](docs/empirical-receipts.md) — build receipts, validation trajectory
- [FAQ](docs/faq.md)

## Bench

Benchmark methodology lives in [`bench/`](bench/). It's intentionally
reproducible: a public LongMemEval reproduction plus a personal-corpus
eval template you can fill in with your own data. Numbers without
methodology are vibes; the bench is here so you can verify or refute on
your own machine.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All test fixtures are synthetic.
Pre-commit hooks block PII and secret patterns from entering the repo —
read the contributing guide before your first commit so the hooks don't
surprise you.

## License

MIT. See [LICENSE](LICENSE).
