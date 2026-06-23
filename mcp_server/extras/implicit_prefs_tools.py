"""MCP tool for the implicit-preferences store.

Exposes a single read-only tool:

* :func:`list_implicit_preferences` — return operator preferences inferred
  from behavioral patterns (Edit vs Write frequency, shell command choices,
  emoji absence, recurring vocabulary). Only preferences that have crossed
  the promotion threshold (evidence_sessions ≥ 5, evidence_projects ≥ 3,
  stability ≥ 7 days) are stored; ``min_confidence`` filters further.

The table is written by the indexer via
:mod:`index.implicit_preferences.persist_implicit_preferences`; this module
never writes and degrades gracefully when the table is absent.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3

from mcp_server.server import get_conn, mcp
from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (mirrors decisions_tools pattern)
# ---------------------------------------------------------------------------


def _index_missing_error() -> list[dict]:
    return [
        {
            "error": (
                "index not initialized; run `total-recall index` to build it"
            )
        }
    ]


def _table_exists(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='implicit_preferences'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_implicit_preferences(
    min_confidence: float = 0.6,
    category: str | None = None,
) -> list[dict]:
    """Return operator preferences inferred from behavioral patterns.

    These are derived from *what the operator consistently does* across
    sessions — not from explicit statements. Examples: always using Edit
    over Write, always invoking ``uv`` rather than ``pip``, never using
    emoji. Only patterns that have been observed across ≥5 sessions and
    ≥3 projects over ≥7 days are surfaced.

    Args:
        min_confidence: Minimum confidence threshold (0.0–1.0). Default 0.6.
        category: Optional filter — one of ``edit_strategy``,
            ``shell_command``, ``format``, ``vocabulary``.

    Returns a list of dicts, each with keys: ``category``, ``value``,
    ``confidence``, ``evidence_sessions``, ``evidence_projects``,
    ``contradiction_count``, ``sample_phrases``.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()
    try:
        if not _table_exists(conn):
            return [
                {
                    "note": (
                        "implicit_preferences table not present yet; "
                        "reindex with implicit-preferences extractor enabled"
                    ),
                    "preferences": [],
                }
            ]

        try:
            from index.implicit_preferences import get_implicit_preferences
            rows = get_implicit_preferences(
                conn,
                min_confidence=min_confidence,
                category=category,
            )
        except sqlite3.Error as e:
            log.exception("list_implicit_preferences sql failure")
            return [{"error": f"list_implicit_preferences failed: {e!r}"}]

        return rows
    finally:
        with contextlib.suppress(Exception):
            conn.close()


__all__ = ["list_implicit_preferences"]
