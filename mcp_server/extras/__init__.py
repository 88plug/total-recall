"""MCP tool surfaces for extractor-specific recall flows.

Each module here registers tools onto the shared :data:`mcp_server.server.mcp`
instance via the ``@mcp.tool()`` decorator at import time. The orchestrator
imports them from :mod:`mcp_server.server` so the side-effect runs once.
"""

from mcp_server.extras import implicit_prefs_tools as _implicit_prefs_tools  # noqa: F401
from mcp_server.extras import satisfaction_tools as _satisfaction_tools  # noqa: F401
from mcp_server.extras import workflow_tools as _workflow_tools  # noqa: F401
