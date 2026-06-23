"""MCP tool: ``get_workflow_profile``.

Reads the cached workflow profile from the ``workflow_profile`` SQLite table
and returns it as a flat dict the model can consult at session start.

The tool is cheap — a single SELECT against a read-only handle. It does NOT
re-mine the corpus; that happens during ``total-recall index`` runs via the
workflow extractor.

Companion to :mod:`mcp_server.extras.operator_tools` (WHO the operator is)
and :mod:`mcp_server.extras.voice_tools` (HOW they talk). This one tells
sessions HOW they work: fan-out patterns, autonomy level, planning idiom,
peak hours, and subagent adoption.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from mcp_server.server import DB_PATH, get_conn, mcp
from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)


def _index_missing_error() -> dict[str, Any]:
    return {
        "error": "index not initialized; run `total-recall index` to build it",
        "db_path": str(DB_PATH),
    }


@mcp.tool(title="Get Workflow Profile", annotations=ToolAnnotations(readOnlyHint=True))
def get_workflow_profile() -> dict:
    (
        "Return the operator's measured workflow behaviour — fan-out vocabulary, autonomy "
        "score, interrupt rate, planning idiom, peak working hours, session shape, and subagent "
        "adoption. Use at session start to calibrate execution style: high autonomy_score means "
        "execute without asking; a strong planning_idiom signals the operator's preferred "
        "decomposition frame."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    try:
        try:
            from index.workflow import get_workflow
        except Exception as exc:
            log.warning("index.workflow unavailable: %s", exc)
            return {"error": f"index.workflow not available: {exc!r}"}

        try:
            profile = get_workflow(conn)
        except Exception as exc:
            log.exception("get_workflow_profile() query failed")
            return {"error": f"get_workflow_profile failed: {exc!r}"}

        # Empty table → return a stable "not yet measured" error shape.
        if not profile or set(profile.keys()) <= {"_updated_ts"}:
            return {
                "error": (
                    "workflow profile not yet measured; run `total-recall index` "
                    "after upgrading to a build with the workflow extractor enabled"
                ),
                "db_path": str(DB_PATH),
            }

        return profile
    finally:
        with contextlib.suppress(Exception):
            conn.close()


__all__ = ["get_workflow_profile"]
