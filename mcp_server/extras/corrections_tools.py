"""MCP tools for the ``model_correction`` extraction kind.

These two tools are the *highest-priority* recall surfaces in the project —
they let the model look up past operator pushback before making a suggestion,
which is the single most effective preventer of repeat mistakes.

Both tools are read-only, degrade gracefully when the SQLite index is absent
or when the WT-4 ``index.query`` module hasn't landed yet on the current
branch, and never raise into the FastMCP transport.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (kept local to avoid coupling with mcp_server.tools internals).
# ---------------------------------------------------------------------------


def _index_missing_error() -> list[dict]:
    return [
        {
            "error": "index not initialized; run `total-recall index` to build it",
            "db_path": str(DB_PATH),
        }
    ]


def _load_query_module():
    try:
        from index import query  # type: ignore
    except Exception as e:  # ImportError or any submodule blow-up
        log.warning("index.query unavailable: %s", e)
        return None
    return query


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


def _row_to_correction(row: Any) -> dict:
    """Coerce a sqlite row / mapping into the documented correction shape."""
    if hasattr(row, "keys") or isinstance(row, dict):
        d = dict(row)
    else:
        try:
            d = dict(vars(row))
        except TypeError:
            d = {"value": str(row)}

    context = d.get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (ValueError, TypeError):
            context = {}

    severity = context.get("severity")
    if severity is None:
        # Older records may carry severity only in `score`.
        severity = d.get("score")

    return {
        "correction": context.get("correction") or d.get("content"),
        "rejected_approach": context.get("rejected_approach"),
        "preceding_uuid": context.get("preceding_uuid"),
        "severity": severity,
        "session_id": d.get("session_id"),
        "cwd": d.get("cwd"),
        "ts": _ts_to_iso(d.get("ts")),
        "score": d.get("score"),
    }


def _current_cwd() -> str:
    import os

    return os.environ.get("PWD") or os.getcwd()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(title="Recall Corrections About", annotations=ToolAnnotations(readOnlyHint=True))
def recall_corrections_about(
    topic: str,
    scope: Literal["this_cwd", "all_projects"] = "all_projects",
    limit: int = 10,
) -> list[dict]:
    (
        "Return past operator corrections matching a topic. Includes the rejected approach "
        "(what the model previously did that the operator pushed back on) so the current turn "
        "can avoid repeating it. HIGH-PRIORITY: call before suggesting any default provider, "
        "library, pattern, or approach the user might have previously rejected."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    cwd_filter = _current_cwd() if scope == "this_cwd" else None

    try:
        try:
            hits = query_mod.search_extractions(
                conn,
                query=topic,
                cwd=cwd_filter,
                kind="model_correction",
                limit=limit,
            )
        except TypeError:
            # Older query API w/o `kind` kwarg — fall back and filter in Python.
            hits = query_mod.search_extractions(
                conn,
                query=topic,
                cwd=cwd_filter,
                limit=limit,
            )
            hits = [
                h
                for h in hits
                if (h["kind"] if hasattr(h, "keys") else getattr(h, "kind", None))
                == "model_correction"
            ]
    except Exception as e:
        log.exception("recall_corrections_about() failed")
        return [{"error": f"recall_corrections_about failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out = [_row_to_correction(h) for h in hits]
    # Sort severity DESC then ts DESC. None-safe.
    out.sort(
        key=lambda r: (
            r.get("severity") if isinstance(r.get("severity"), (int, float)) else -1.0,
            r.get("ts") or "",
        ),
        reverse=True,
    )
    return out[:limit]


@mcp.tool(title="Get Recent Corrections", annotations=ToolAnnotations(readOnlyHint=True))
def get_recent_corrections(
    cwd: str | None = None,
    since_days: int = 30,
    limit: int = 5,
) -> list[dict]:
    (
        "Return recent operator corrections, useful at session start to know what the model has "
        "been doing wrong lately. Pulls from the corrections index (kind='model_correction'). "
        "Params: cwd scopes to a project (None = all projects); since_days bounds how far back to "
        "look (default 30); limit caps how many are returned (default 5, most recent first). "
        "Returns a list of dicts each describing one correction (the corrected behavior, context, "
        "and timestamp); returns an error envelope if the index is missing."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(since_days)))
    cutoff_iso = cutoff.isoformat()

    try:
        try:
            hits = query_mod.search_extractions(
                conn,
                query="",
                cwd=cwd,
                kind="model_correction",
                limit=max(limit * 4, limit),
            )
        except TypeError:
            hits = query_mod.search_extractions(
                conn,
                query="",
                cwd=cwd,
                limit=max(limit * 4, limit),
            )
            hits = [
                h
                for h in hits
                if (h["kind"] if hasattr(h, "keys") else getattr(h, "kind", None))
                == "model_correction"
            ]
    except sqlite3.Error as e:
        log.exception("get_recent_corrections() sql failure")
        return [{"error": f"get_recent_corrections failed: {e!r}"}]
    except Exception as e:
        log.exception("get_recent_corrections() failed")
        return [{"error": f"get_recent_corrections failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out: list[dict] = []
    for h in hits:
        row = _row_to_correction(h)
        ts = row.get("ts")
        # `since_days` is best-effort: if a row has a non-ISO ts we keep it
        # rather than throw away signal.
        if isinstance(ts, str) and ts and ts < cutoff_iso:
            continue
        out.append(row)

    out.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return out[:limit]


__all__ = [
    "recall_corrections_about",
    "get_recent_corrections",
]
