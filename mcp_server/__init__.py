"""FastMCP stdio server exposing the total-recall index as tools.

This package is the live-query surface of `total-recall`. The model can call
its tools mid-conversation to look up prior decisions, user corrections,
failed approaches, and other extracted memories from past Claude Code
sessions stored in the local SQLite index.

The server is intentionally thin: every tool delegates to `index.query` (or
`vec.rrf.hybrid_search` when the optional vector layer is installed) and
formats results as plain `list[dict]` so FastMCP can auto-generate JSON
schema from the type hints.

Public surface:

- :data:`mcp_server.server.mcp` — the configured ``FastMCP`` instance.
- :func:`mcp_server.server.main` — stdio entrypoint (`python -m mcp_server`).
- :mod:`mcp_server.tools` — tool implementations.
- :mod:`mcp_server.resources` — optional MCP resources (e.g. session digest).

Sandbox:

- Read-only against the SQLite index.
- Never writes to ``~/.claude/projects/``.
- DB path is controlled by ``$TOTAL_RECALL_DB_DIR`` (default
  ``${CLAUDE_PLUGIN_DATA}`` or ``~/.local/share/total-recall``).
"""

from mcp_server.server import main, mcp

__all__ = ["mcp", "main"]
