"""MCP tool surfaces for extractor-specific recall flows.

Each module here registers tools onto the shared :data:`mcp_server.server.mcp`
instance via the ``@mcp.tool()`` decorator at import time. The orchestrator
imports them from :mod:`mcp_server.server` so the side-effect runs once.
"""
