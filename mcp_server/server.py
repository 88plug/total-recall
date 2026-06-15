"""FastMCP server instance + lifecycle for ``total-recall``.

The server runs over stdio (the only transport Claude Code child-process MCPs
need). Tools are registered at import time by :mod:`mcp_server.tools`;
resources by :mod:`mcp_server.resources`. Both modules import :data:`mcp`
from here so they can decorate their handlers.

Path resolution order for the SQLite index, highest to lowest precedence:

1. ``$TOTAL_RECALL_DB_DIR`` (set by ``.mcp.json``)
2. ``$CLAUDE_PLUGIN_DATA`` (set by Claude Code itself)
3. ``~/.local/share/total-recall`` (dev/test fallback)

The DB filename is always ``index.db`` (matches what :mod:`index.db` writes).

Connections are opened **read-only** via SQLite URI mode (`mode=ro`) so a
crashing tool cannot corrupt the index mid-conversation. If the file does not
exist yet (user hasn't run ``total-recall index``), tools degrade gracefully
to empty results rather than raising — see :mod:`mcp_server.tools`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------


def _resolve_db_dir() -> Path:
    """Resolve the directory the SQLite index lives in.

    See module docstring for precedence. Always returns an expanded absolute
    path; does not create the directory (read-only consumer).
    """
    raw = (
        os.environ.get("TOTAL_RECALL_DB_DIR")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or "~/.local/share/total-recall"
    )
    return Path(raw).expanduser().resolve()


DB_DIR: Path = _resolve_db_dir()
DB_PATH: Path = DB_DIR / "index.db"


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------


INSTRUCTIONS = (
    "Cross-session memory for Claude Code. Use these tools when the user "
    "references prior work, asks about past decisions, or you need to avoid "
    "repeating a corrected mistake. The data is read-only and sourced from "
    "~/.claude/projects/*.jsonl on this machine — indexed locally by the "
    "total-recall plugin. Prefer `recall(topic, ...)` for general lookup; "
    "fall back to `search_messages(...)` only if `recall` returns nothing."
)


mcp = FastMCP("total-recall", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# DB connection helper
# ---------------------------------------------------------------------------


def get_conn() -> sqlite3.Connection | None:
    """Open the recall index read-only.

    Returns ``None`` if the DB file does not exist — tools treat that as
    "index not initialized" and return an empty / error-marked result rather
    than raising. Returns a live :class:`sqlite3.Connection` otherwise, with
    ``row_factory = sqlite3.Row`` so callers get dict-like rows.

    The connection is opened in URI ``mode=ro`` so writes from this server
    are impossible by construction. SQLite's FTS5 reads work fine on a
    read-only handle.
    """
    if not DB_PATH.exists():
        log.debug("recall DB missing at %s", DB_PATH)
        return None
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:  # pragma: no cover - filesystem races
        log.warning("failed to open recall DB at %s: %s", DB_PATH, e)
        return None


# ---------------------------------------------------------------------------
# Register tools / resources by importing the modules.
# ---------------------------------------------------------------------------
#
# Order matters only insofar as both modules need `mcp` from this file; the
# imports below are intentional side-effect imports.

from mcp_server import resources as _resources  # noqa: E402,F401
from mcp_server import tools as _tools  # noqa: E402,F401

# v0.3 operator-aware MCP surfaces (I1-I10). Side-effect imports register the
# @mcp.tool() decorators at server boot. Each is independently graceful — a
# missing index table degrades that one tool's response, never the others.
from mcp_server.extras import bans_tools as _bans_tools  # noqa: E402,F401
from mcp_server.extras import corrections_tools as _corrections_tools  # noqa: E402,F401
from mcp_server.extras import decisions_tools as _decisions_tools  # noqa: E402,F401
from mcp_server.extras import escalation_tools as _escalation_tools  # noqa: E402,F401
from mcp_server.extras import goals_tools as _goals_tools  # noqa: E402,F401
from mcp_server.extras import ontology_tools as _ontology_tools  # noqa: E402,F401
from mcp_server.extras import operator_context_tools as _operator_context_tools  # noqa: E402,F401
from mcp_server.extras import operator_tools as _operator_tools  # noqa: E402,F401
from mcp_server.extras import recall_targeted_tools as _recall_targeted_tools  # noqa: E402,F401
from mcp_server.extras import rhetoric_tools as _rhetoric_tools  # noqa: E402,F401
from mcp_server.extras import voice_tools as _voice_tools  # noqa: E402,F401
from mcp_server.extras import workflow_tools as _workflow_tools  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the FastMCP stdio loop.

    Blocks until the parent process (Claude Code) closes stdin. Logs go to
    stderr by convention — never stdout, because stdout is the JSON-RPC wire.
    """
    logging.basicConfig(
        level=os.environ.get("TOTAL_RECALL_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("starting total-recall MCP server (DB=%s)", DB_PATH)
    mcp.run()  # stdio is the default transport


if __name__ == "__main__":  # pragma: no cover - module is normally imported
    main()
