"""MCP tool for the ``truth_assertion`` extraction kind.

Surfaces past moments where the operator pushed back *hard* — restatements,
quote-backs, standing rules, session-log appeals, drift callouts, capability
insults, and verify-yourself pushes (see
:mod:`extractors.truth_rhetoric` for the full taxonomy).

Lets the model answer questions like "have I ever been quoted back to myself
about X?" or "has the operator ever invoked the session logs on this topic?"
before repeating a pattern that already triggered a hard correction.

Tool is read-only, degrades gracefully when the SQLite index or the
``index.query`` module are absent, and never raises into the FastMCP
transport.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime
from typing import Any

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


_VALID_CATEGORIES = {
    "restatement",
    "quote_back",
    "standing_rule",
    "past_logs_appeal",
    "drift_callout",
    "capability_insult",
    "verify_yourself_push",
}


# ---------------------------------------------------------------------------
# Local helpers (mirrors corrections_tools to avoid cross-module coupling).
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


def _row_to_assertion(row: Any) -> dict:
    """Coerce a sqlite row / mapping into the documented truth-assertion shape."""
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
        severity = d.get("score")

    return {
        "category": context.get("category"),
        "assertion": d.get("content"),
        "preceding_assistant_uuid": context.get("preceding_assistant_uuid"),
        "preceding_assistant_text_excerpt": context.get("preceding_assistant_text_excerpt"),
        "severity": severity,
        "session_id": d.get("session_id"),
        "cwd": d.get("cwd"),
        "ts": _ts_to_iso(d.get("ts")),
        "score": d.get("score"),
    }


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@mcp.tool(title="Get Past Truth Assertions", annotations=ToolAnnotations(readOnlyHint=True))
def get_past_truth_assertions(
    topic: str | None = None,
    category: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Return past moments where the operator pushed back hard. Categories: restatement,
    quote_back, standing_rule, past_logs_appeal, drift_callout, capability_insult,
    verify_yourself_push. Use this to recognize when the model is about to repeat a pattern
    that already triggered a hard correction.
    """
    if category is not None and category not in _VALID_CATEGORIES:
        return [{"error": f"unknown category {category!r}; valid: {sorted(_VALID_CATEGORIES)}"}]

    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        with contextlib.suppress(Exception):
            conn.close()
        return [{"error": "index.query module not available on this branch"}]

    # Over-fetch so post-filtering by category still leaves us with `limit` rows.
    fetch_limit = max(limit * 4, limit)

    try:
        hits = None
        qtext = (topic or "").strip()
        if qtext:
            try:
                from vec.rrf import try_hybrid_search

                hits = try_hybrid_search(
                    conn, qtext, limit=fetch_limit, kind="truth_assertion",
                )
            except Exception:
                hits = None
        if not hits:
            try:
                hits = query_mod.search_extractions(
                    conn,
                    query=topic or "",
                    kind="truth_assertion",
                    limit=fetch_limit,
                )
            except TypeError:
                # Older query API w/o `kind` kwarg — filter in Python.
                hits = query_mod.search_extractions(
                    conn,
                    query=topic or "",
                    limit=fetch_limit,
                )
                hits = [
                    h
                    for h in hits
                    if (h["kind"] if hasattr(h, "keys") else getattr(h, "kind", None))
                    == "truth_assertion"
                ]
    except Exception as e:
        log.exception("get_past_truth_assertions() failed")
        return [{"error": f"get_past_truth_assertions failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out = [_row_to_assertion(h) for h in hits]

    if category is not None:
        out = [r for r in out if r.get("category") == category]

    # Severity DESC, then ts DESC. None-safe.
    out.sort(
        key=lambda r: (
            r.get("severity") if isinstance(r.get("severity"), (int, float)) else -1.0,
            r.get("ts") or "",
        ),
        reverse=True,
    )
    return out[:limit]


__all__ = ["get_past_truth_assertions"]
