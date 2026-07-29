"""Stdio entrypoint for the total-recall MCP server.

Usage:

    python -m mcp_server

Claude Code spawns this as a child process per-session via the plugin's
``.mcp.json``. We simply delegate to :func:`mcp_server.server.main`, which
runs the MCP stdio loop and never returns until the parent closes stdin.
"""

from __future__ import annotations

from mcp_server.server import main

if __name__ == "__main__":
    main()
