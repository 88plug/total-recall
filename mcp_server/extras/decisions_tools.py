"""MCP tools for the standing-decisions store.

Two read-only surfaces:

* :func:`list_standing_decisions` — broad sweep filtered by ``topic`` and
  ``scope``; the model uses this when it wants the whole roster of
  operator positions (good at SessionStart, or when picking any default).
* :func:`get_decision_for_topic` — quick lookup for a single canonical
  topic (``cloud_provider``, ``billing_rail``, …) — preferred over
  ``recall`` when the agent already knows the topic name.

Both tools degrade gracefully when the index file is missing or the
``standing_decisions`` table hasn't been bootstrapped yet — they never
raise into the FastMCP transport. The table is created lazily by the
indexer via :mod:`index.decisions.ensure_schema`; this module does not
try to create it from a read-only connection.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

from mcp_server.server import DB_PATH, get_conn, mcp
from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_missing_error() -> list[dict]:
    return [
        {
            "error": "index not initialized; run `total-recall index` to build it",
            "db_path": str(DB_PATH),
        }
    ]


def _current_cwd() -> str:
    return os.environ.get("PWD") or os.getcwd()


def _ts_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys") or isinstance(row, dict):
        d = dict(row)
    else:
        try:
            d = dict(vars(row))
        except TypeError:
            d = {"value": str(row)}
    for key in ("first_asserted_ts", "last_reasserted_ts", "reversed_at_ts"):
        if key in d:
            d[key] = _ts_to_iso(d[key])
    return d


def _table_exists(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='standing_decisions'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_standing_decisions(
    topic: str | None = None,
    scope: str | None = None,
) -> list[dict]:
    (
        "Return standing decisions the operator has made (e.g. provider-a > provider-b, "
        "billing-provider-a > billing-provider-b). Use BEFORE suggesting any default — if a "
        "decision exists, honor it. Filter by topic ('cloud_provider', 'billing_rail', etc.) or "
        "scope ('global' or cwd)."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()
    try:
        if not _table_exists(conn):
            return [
                {
                    "error": (
                        "standing_decisions table not present; reindex "
                        "with the standing_decisions extractor enabled"
                    )
                }
            ]

        clauses: list[str] = []
        params: list[Any] = []
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM standing_decisions
                {where}
                ORDER BY assertion_count DESC,
                         COALESCE(last_reasserted_ts, 0) DESC
                LIMIT 200
                """,
                params,
            ).fetchall()
        except sqlite3.Error as e:
            log.exception("list_standing_decisions sql failure")
            return [{"error": f"list_standing_decisions failed: {e!r}"}]
        return [_row_to_dict(r) for r in rows]
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_decision_for_topic(
    topic: str,
    cwd: str | None = None,
) -> dict | None:
    (
        "Quick lookup: 'what did the operator decide about X for this project?' Returns None if "
        "no decision recorded."
    )
    conn = get_conn()
    if conn is None:
        # Mirror the list_-tool error envelope rather than returning None,
        # so the caller can distinguish "no decision" from "no index".
        err = _index_missing_error()
        return err[0] if err else None
    try:
        if not _table_exists(conn):
            return {
                "error": (
                    "standing_decisions table not present; reindex "
                    "with the standing_decisions extractor enabled"
                )
            }

        target_scope = cwd if cwd is not None else _current_cwd()
        # Prefer a project-scoped row that matches this cwd; fall back to
        # any project-scoped row; then to a global row. This mirrors how
        # the SessionStart surface should reason about it.
        try:
            row = conn.execute(
                """
                SELECT * FROM standing_decisions
                 WHERE topic = ? AND scope = ?
                 ORDER BY assertion_count DESC,
                          COALESCE(last_reasserted_ts, 0) DESC
                 LIMIT 1
                """,
                (topic, target_scope),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM standing_decisions
                     WHERE topic = ? AND scope = 'global'
                     ORDER BY assertion_count DESC,
                              COALESCE(last_reasserted_ts, 0) DESC
                     LIMIT 1
                    """,
                    (topic,),
                ).fetchone()
            if row is None:
                # Last resort: any decision under this topic.
                row = conn.execute(
                    """
                    SELECT * FROM standing_decisions
                     WHERE topic = ?
                     ORDER BY assertion_count DESC,
                              COALESCE(last_reasserted_ts, 0) DESC
                     LIMIT 1
                    """,
                    (topic,),
                ).fetchone()
        except sqlite3.Error as e:
            log.exception("get_decision_for_topic sql failure")
            return {"error": f"get_decision_for_topic failed: {e!r}"}
        return _row_to_dict(row) if row is not None else None
    finally:
        with contextlib.suppress(Exception):
            conn.close()


__all__ = [
    "list_standing_decisions",
    "get_decision_for_topic",
]
