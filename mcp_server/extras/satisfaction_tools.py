"""MCP tool: ``get_satisfaction_profile``.

Reads the cached satisfaction cross-tab from the ``satisfaction_profile``
SQLite table and returns a summary dict. The table is populated by
:mod:`extractors.satisfaction` during ``total-recall index`` runs.

Bidirectional model
-------------------
The profile captures reactions in BOTH directions — praise (``praise_quality``,
``praise_launch``, ``silent_accept``) AND frustration (``frustration_drift``,
``frustration_broke``, ``frustration_wtf``, ``frustration_scope``,
``frustration_verbosity``) — and pairs each reaction with the shape of the
assistant turn that preceded it.

This lets the model answer: "when I wrote a long prose reply, did the
operator tend to accept it quietly or push back?" The ``top_praise_behavior``
and ``top_frustration_behavior`` keys give the fast read.

Calibration asymmetry
---------------------
Frustration is loud and explicit (the operator types something), so it is
over-represented relative to satisfaction.  Silent acceptance (the operator
types ≤2 words that are not a known signal) is the closest proxy for genuine
approval without any verbal acknowledgment, but it is still an undercount.
Interpret ``total_frustration_count / total_praise_count`` ratios with this
bias in mind.
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


@mcp.tool(title="Get Satisfaction Profile", annotations=ToolAnnotations(readOnlyHint=True))
def get_satisfaction_profile() -> dict:
    """Return the bidirectional satisfaction cross-tab: which assistant
    behaviors earn praise vs. frustration.

    The matrix maps each reaction category (e.g. ``praise_quality``,
    ``frustration_verbosity``) to a dict of ``{ai_behavior: count}``.
    Top-line keys ``top_praise_behavior`` and ``top_frustration_behavior``
    give the single AI behavior most often followed by praise/frustration.

    Calibration note: frustration signals are explicit (the operator typed
    something); silent acceptance is inferred and under-counted.  Use the
    cross-tab for direction, not precise ratios.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    try:
        try:
            from index.satisfaction import get_satisfaction_summary
        except Exception as exc:
            log.warning("index.satisfaction unavailable: %s", exc)
            return {"error": f"index.satisfaction not available: {exc!r}"}

        try:
            summary = get_satisfaction_summary(conn)
        except Exception as exc:
            log.exception("get_satisfaction_profile() query failed")
            return {"error": f"get_satisfaction_profile failed: {exc!r}"}

        if not summary.get("matrix") and summary.get("sample_size", 0) == 0:
            return {
                "error": (
                    "satisfaction profile not yet measured; run `total-recall index` "
                    "after upgrading to a build with the satisfaction extractor enabled"
                ),
                "db_path": str(DB_PATH),
            }

        return summary
    finally:
        with contextlib.suppress(Exception):
            conn.close()


__all__ = ["get_satisfaction_profile"]
