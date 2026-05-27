"""Console-script entry point for the total-recall MCP server.

Exposes ``total-recall-mcp`` as a top-level command so users can launch the
server with ``uvx total-recall-mcp`` (the form OpenCode and other MCP hosts
recommend in ``.mcp.json``) instead of having to know the internal module
path ``python -m mcp_server``.

This module is intentionally a thin wrapper: it only re-exports
:func:`mcp_server.server.main`, which blocks on the FastMCP stdio loop until
the parent process closes stdin. All real logic lives in :mod:`mcp_server`.
"""

from __future__ import annotations

from mcp_server.server import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
