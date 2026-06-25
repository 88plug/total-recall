"""MCP tool: ``get_operator_profile``.

Reads the cached operator profile from the ``operator_profile`` SQLite
table and returns it as a flat dict suitable for the model to use at
session start.

This tool is *cheap* — a single ``SELECT * FROM operator_profile`` against
a read-only handle. It deliberately does NOT re-mine the corpus; that
happens during ``total-recall index`` runs.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


def _index_missing_error() -> dict[str, Any]:
    return {
        "error": "index not initialized; run `total-recall index` to build it",
        "db_path": str(DB_PATH),
    }


@mcp.tool(title="Get Operator Profile", annotations=ToolAnnotations(readOnlyHint=True))
def get_operator_profile() -> dict:
    (
        "Return the operator's identity profile (name, handles, machines, vendor preferences, "
        "philosophy). Use at session start to know WHO is asking and what they prefer. Cheap, "
        "reads cached profile from the index — does NOT re-mine the corpus."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    try:
        # Defensive import: keeps the MCP server importable even when the
        # ``index.operator`` module is not on the current branch.
        try:
            from index.operator import get_profile
        except Exception as exc:  # pragma: no cover — import error path
            log.warning("index.operator unavailable: %s", exc)
            return {"error": f"index.operator not available: {exc!r}"}

        try:
            profile = get_profile(conn)
        except Exception as exc:
            log.exception("get_operator_profile() query failed")
            return {"error": f"get_operator_profile failed: {exc!r}"}

        # Empty DB / table → return a stable empty shape rather than
        # bare {}; that way callers can branch on `if not profile.get("name")`.
        if not profile or set(profile.keys()) <= {"_confidence", "_sources"}:
            return {
                "error": (
                    "operator profile not yet mined; run `total-recall index` "
                    "after upgrading to a build with the operator_profile "
                    "extractor enabled"
                ),
                "db_path": str(DB_PATH),
            }

        return profile
    finally:
        with contextlib.suppress(Exception):
            conn.close()
