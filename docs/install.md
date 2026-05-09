# Install

aurochs-recall is a Python package. Requires Python 3.13 or newer.

## Core install

```bash
pip install aurochs-recall
```

That gives you SQLite FTS5 retrieval, the four memory layers, and the CLI.
For semantic / hybrid retrieval you'll want one of the extras below.

## Extras

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
| `[all]`          | Everything above                                                        |

```bash
pip install "aurochs-recall[chroma,embeddings]"
```

The MCP server ships with the base install; no extra needed for Claude Code / IDE clients to wire it up.

## First run

```bash
recall init
```

This walks you through source discovery (Claude Code projects, claude.ai
exports, ChatGPT exports, markdown vaults) and writes
`~/.config/aurochs-recall/sources.toml` (Linux/macOS) or
`%APPDATA%\aurochs-recall\sources.toml` (Windows).

The first ingest runs immediately after `init` completes. Expect a few
minutes for a typical corpus; subsequent ingests are incremental
(per-file `index_state` tracks what's already been processed).

## Where the data lives

| Path                                                          | What                            |
| ------------------------------------------------------------- | ------------------------------- |
| `~/.local/share/aurochs-recall/recall.db` (Linux)             | The SQLite database (drawers + index + graph + access_log) |
| `~/Library/Application Support/aurochs-recall/recall.db` (macOS) | Same                          |
| `%LOCALAPPDATA%\aurochs-recall\recall.db` (Windows)           | Same                            |
| `~/.config/aurochs-recall/sources.toml`                       | Sources config                  |

(Override with `--db-path` on any command.)

## Verifying install

```bash
recall status
```

Should report your schema version, drawer count, WAL size, and lockfile
state. If it doesn't, see [Failure modes](failure-modes.md).
