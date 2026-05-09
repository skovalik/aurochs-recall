"""MCP server entrypoint package for aurochs-recall.

Installed via the ``[mcp]`` extra. Exposes 5 tools to Claude / IDE
clients that speak the Model Context Protocol:

  - recall_search       — main search entrypoint (BM25 / hybrid)
  - recall_drawer       — fetch full drawer by uid
  - recall_status       — DB stats including wal_size_pages
  - recall_graph_query  — knowledge graph entity / relationship lookup
  - recall_forget       — soft-delete a drawer by drawer_uid prefix

The server is a thin shim over ``aurochs_recall.cli.main`` and
``aurochs_recall.core``: the CLI is the source of truth, so MCP
behavior follows CLI behavior by construction.

Run via:
    python -m aurochs_recall.mcp.server

or wire into an MCP client config that points stdio at the same module.
"""

from __future__ import annotations

__all__ = ["server"]
