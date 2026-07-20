# Changelog

All notable changes to aurochs-recall.

## 0.2.0 (2026-07-19)

- Add the `recall-mcp` console script as the canonical MCP server entrypoint (stdio). The bare `recall` command defaults to the `search` subcommand and never serves MCP; `python -m aurochs_recall.mcp.server` still works but is not portable across install environments (found by the first external install, which wired MCP to the bare command).
- The Claude Code plugin now launches its MCP server via `recall-mcp` instead of `python -m aurochs_recall.mcp.server`, so it works wherever the package is installed (pip, uv tool, editable).

## 0.1.0

- Initial release: verbatim drawers, SQLite FTS5 BM25 index, ingest for Claude Code transcripts / claude.ai exports / markdown corpora, knowledge graph, CLI, MCP server, Claude Code plugin.
