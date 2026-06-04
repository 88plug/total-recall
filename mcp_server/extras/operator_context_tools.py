"""Unified operator-context MCP tool — the *one call* at session start.

This module exposes a single tool, :func:`get_operator_context`, which
bundles the highest-leverage findings from the I1-I9 sister tracks (identity,
goals, standing decisions, bans, voice cheat sheet, recent model corrections,
machine inventory) into a single ~2KB payload suitable for injection into
the SessionStart envelope as ``additionalContext``.

Design notes:

* **One tool, not seven.** Each I1-I9 surface ships its own MCP tool already
  — this aggregator exists so the SessionStart hook only has to make a
  single round-trip and ship a single bounded blob into the model's context
  window. The per-surface tools remain available for on-demand follow-up.

* **Defensive imports.** Every sister module (``index.goals``,
  ``index.bans``, ``index.voice``, ``index.ontology``, ``index.decisions``,
  ``index.operator``, ``index.query``) is imported inside its own
  ``try/except``. If a track hasn't landed on the current branch yet, that
  section is silently omitted from the payload — the rest still ships. This
  is the same graceful-degradation pattern used by
  ``mcp_server.extras.corrections_tools``.

* **Hard budget.** ``max_chars`` (default 1800) caps the JSON-serialized
  payload. When the budget is tight, sections are dropped in *reverse*
  priority order so the model always gets identity + active goal first.

* **Never raises.** Anything unexpected becomes ``{"error": "..."}`` in the
  returned dict — the SessionStart hook treats any non-empty response as
  emit-able, and the model can decide what to do with a degraded payload.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


# Priority order — highest-value sections appear first in the payload and
# are the last to be evicted when ``max_chars`` is tight. Identity grounds
# the model in *whose* memory this is; the active goal grounds the *what*.
# Standing decisions and bans are the strongest "don't re-debate" signals.
# Voice/corrections/machines are nice-to-have flavor.
_PRIORITY: tuple[str, ...] = (
    "identity",
    "active_goal",
    "standing_decisions",
    "bans",
    "voice",
    "recent_corrections",
    "machines",
)


# ---------------------------------------------------------------------------
# Section collectors — each is wrapped in defensive try/except by the caller.
# ---------------------------------------------------------------------------


def _collect_identity(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return ``{name, handle, email}`` from the operator profile, or None."""
    from index.operator import get_profile  # type: ignore

    profile = get_profile(conn)
    if not profile:
        return None
    out: dict[str, Any] = {}
    # ``get_profile`` returns ``_confidence`` and ``_sources`` siblings; skip them.
    for key in ("name", "handle", "primary_email", "email"):
        val = profile.get(key)
        if val is not None and not isinstance(val, (dict, list)):
            out[key if key != "primary_email" else "email"] = val
    return out or None


def _collect_active_goal(conn: sqlite3.Connection, cwd: str | None) -> dict[str, Any] | None:
    """Return the active project goal for ``cwd`` (or global), or None."""
    from index.goals import get_active_goal_for_cwd  # type: ignore

    goal = get_active_goal_for_cwd(conn, cwd=cwd) if cwd else get_active_goal_for_cwd(conn)
    if not goal:
        return None
    # The goals API returns a ``GoalRow`` dataclass (``.to_dict()`` →
    # ``goal_text`` / ``project`` / ``status`` …); other callers may hand us a
    # plain dict. Normalize both to a compact ``{goal, status, scope}`` shape
    # the model can read, mapping ``goal_text`` → ``goal``.
    if not isinstance(goal, dict) and hasattr(goal, "to_dict"):
        goal = goal.to_dict()
    if isinstance(goal, dict):
        text = goal.get("goal") or goal.get("goal_text")
        out: dict[str, Any] = {}
        if text:
            out["goal"] = text
        for src, dst in (("status", "status"), ("scope", "scope"),
                         ("project", "scope"), ("cwd", "cwd"),
                         ("summary", "summary"),
                         ("last_progress_ts", "ts"), ("ts", "ts")):
            val = goal.get(src)
            if val is not None and dst not in out:
                out[dst] = val
        return out or {"goal": str(goal)}
    return {"goal": str(goal)}


def _collect_standing_decisions(conn: sqlite3.Connection, cwd: str | None) -> list[dict[str, Any]]:
    """Top 3 active standing decisions for the cwd's scope (+ globals)."""
    # Two import paths: the spec-mentioned ``top_decisions_for_scope`` if
    # it exists, otherwise the canonical ``list_decisions`` from
    # ``index.decisions`` which already orders by ``assertion_count DESC``.
    try:
        from index.decisions import top_decisions_for_scope  # type: ignore

        rows = top_decisions_for_scope(conn, cwd=cwd, limit=3)  # type: ignore[call-arg]
    except (ImportError, AttributeError):
        from index.decisions import list_decisions  # type: ignore

        rows = list_decisions(conn, include_reversed=False, limit=3)
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            try:
                row = {k: row[k] for k in row.keys()}  # type: ignore[attr-defined]
            except Exception:
                continue
        out.append(
            {
                k: row[k]
                for k in ("topic", "chose", "over", "rationale", "scope", "assertion_count")
                if k in row and row[k] is not None
            }
        )
    return out


def _collect_bans(conn: sqlite3.Connection, cwd: str | None) -> list[dict[str, Any]]:
    """Top 3 absolute bans (provider / tool / pattern)."""
    from index.bans import top_bans  # type: ignore

    try:
        rows = top_bans(conn, cwd=cwd, limit=3)  # type: ignore[call-arg]
    except TypeError:
        rows = top_bans(conn, limit=3)  # type: ignore[call-arg]
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            try:
                row = {k: row[k] for k in row.keys()}  # type: ignore[attr-defined]
            except Exception:
                continue
        out.append(
            {
                k: row[k]
                for k in ("target", "kind", "reason", "severity", "scope")
                if k in row and row[k] is not None
            }
        )
    return out


def _collect_voice(conn: sqlite3.Connection) -> list[str] | None:
    """Voice cheat sheet — at most 10 short bullet lines."""
    from index.voice import get_voice_summary  # type: ignore

    summary = get_voice_summary(conn)
    if not summary:
        return None
    if isinstance(summary, str):
        lines = [ln.strip(" -*\t") for ln in summary.splitlines() if ln.strip()]
    elif isinstance(summary, list):
        lines = [str(x).strip() for x in summary if str(x).strip()]
    elif isinstance(summary, dict):
        # Accept {"lines": [...]} or treat top-level keys as bullets.
        if isinstance(summary.get("lines"), list):
            lines = [str(x).strip() for x in summary["lines"] if str(x).strip()]
        else:
            lines = [f"{k}: {v}" for k, v in summary.items() if v]
    else:
        return None
    return lines[:10] or None


def _collect_recent_corrections(conn: sqlite3.Connection, cwd: str | None) -> list[dict[str, Any]]:
    """Last 2 model_correction rows for this cwd."""
    from index.query import search_extractions  # type: ignore

    try:
        hits = search_extractions(
            conn, query="", cwd=cwd, kind="model_correction", limit=2
        )
    except TypeError:
        # Older signature w/o ``kind``: fetch and filter.
        hits = search_extractions(conn, query="", cwd=cwd, limit=8)
        hits = [
            h
            for h in hits
            if (h["kind"] if hasattr(h, "keys") else getattr(h, "kind", None))
            == "model_correction"
        ][:2]
    out: list[dict[str, Any]] = []
    for h in hits or []:
        # Hits may be sqlite3.Row, QueryHit dataclass, or dict.
        if hasattr(h, "keys"):
            d = {k: h[k] for k in h.keys()}
            context = d.get("context") or d.get("context_json") or {}
            if isinstance(context, str):
                try:
                    context = json.loads(context)
                except (ValueError, TypeError):
                    context = {}
            out.append(
                {
                    "correction": context.get("correction") or d.get("content"),
                    "rejected_approach": context.get("rejected_approach"),
                    "ts": str(d.get("ts")) if d.get("ts") is not None else None,
                }
            )
        else:
            ctx = getattr(h, "context", {}) or {}
            out.append(
                {
                    "correction": ctx.get("correction") or getattr(h, "content", None),
                    "rejected_approach": ctx.get("rejected_approach"),
                    "ts": str(getattr(h, "ts", "")) or None,
                }
            )
    return out


def _collect_machines(conn: sqlite3.Connection, cwd: str | None) -> list[str]:
    """One-line-each list of machines relevant to ``cwd``."""
    from index.ontology import machines_for_cwd  # type: ignore

    rows = machines_for_cwd(conn, cwd=cwd) if cwd else machines_for_cwd(conn)
    out: list[str] = []
    for row in rows or []:
        if isinstance(row, str):
            out.append(row)
            continue
        if hasattr(row, "keys"):
            d = {k: row[k] for k in row.keys()}
        elif isinstance(row, dict):
            d = row
        else:
            out.append(str(row))
            continue
        # One-liner: "name (role) — host"
        parts = [str(d.get("name") or d.get("hostname") or "?")]
        role = d.get("role") or d.get("kind")
        if role:
            parts.append(f"({role})")
        host = d.get("host") or d.get("addr")
        if host:
            parts.append(f"— {host}")
        out.append(" ".join(parts))
    return out


# ---------------------------------------------------------------------------
# Payload builder.
# ---------------------------------------------------------------------------


def _serialize(sections: dict[str, Any]) -> str:
    """Stable, compact JSON for budgeting and emission."""
    return json.dumps(sections, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _build_payload(sections: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Order by priority, then evict-from-the-tail until under budget."""
    # Filter out empty sections so they never take up budget at all.
    pruned: dict[str, Any] = {}
    for key in _PRIORITY:
        val = sections.get(key)
        if val in (None, "", [], {}):
            continue
        pruned[key] = val

    # If we already fit, ship it.
    if len(_serialize(pruned)) <= max_chars:
        return pruned

    # Drop sections from the tail of _PRIORITY until we fit.
    keys_in_order = [k for k in _PRIORITY if k in pruned]
    while keys_in_order and len(_serialize(pruned)) > max_chars:
        victim = keys_in_order.pop()
        pruned.pop(victim, None)

    # Last-resort: even identity alone may exceed (pathological); truncate.
    if len(_serialize(pruned)) > max_chars:
        for k in list(pruned):
            v = pruned[k]
            if isinstance(v, str) and len(v) > 200:
                pruned[k] = v[: max(80, max_chars // 4)] + "…"
        # If still over, fall back to a one-key payload.
        if len(_serialize(pruned)) > max_chars and pruned:
            first = next(iter(pruned))
            pruned = {first: pruned[first]}

    return pruned


def _current_cwd(cwd: str | None) -> str | None:
    if cwd:
        return cwd
    return os.environ.get("PWD") or os.getcwd() or None


# ---------------------------------------------------------------------------
# The tool.
# ---------------------------------------------------------------------------


@mcp.tool()
def get_operator_context(cwd: str | None = None, max_chars: int = 1800) -> dict:
    """Return a unified operator-context payload for the current session.

    HIGH-PRIORITY at session start: a single call that pulls identity,
    active goal for this cwd, top standing decisions, top bans, voice
    cheat sheet, recent model corrections, and the machine inventory.
    Total response is capped at ~``max_chars`` so it fits in the
    SessionStart ``additionalContext`` envelope without bloating the
    context window.
    """
    conn = get_conn()
    if conn is None:
        return {
            "error": "index not initialized; run `total-recall index`",
            "db_path": str(DB_PATH),
        }

    cwd = _current_cwd(cwd)
    sections: dict[str, Any] = {}

    # Each collector is fully isolated: an import error, schema mismatch, or
    # SQL failure in one section never blocks any other section. The pattern
    # is `try: sections[key] = collector(...) except Exception: log.debug(...)`.

    try:
        ident = _collect_identity(conn)
        if ident:
            sections["identity"] = ident
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: identity unavailable: %s", e)

    try:
        goal = _collect_active_goal(conn, cwd)
        if goal:
            sections["active_goal"] = goal
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: active_goal unavailable: %s", e)

    try:
        decisions = _collect_standing_decisions(conn, cwd)
        if decisions:
            sections["standing_decisions"] = decisions
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: standing_decisions unavailable: %s", e)

    try:
        bans = _collect_bans(conn, cwd)
        if bans:
            sections["bans"] = bans
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: bans unavailable: %s", e)

    try:
        voice = _collect_voice(conn)
        if voice:
            sections["voice"] = voice
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: voice unavailable: %s", e)

    try:
        corrections = _collect_recent_corrections(conn, cwd)
        if corrections:
            sections["recent_corrections"] = corrections
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: recent_corrections unavailable: %s", e)

    try:
        machines = _collect_machines(conn, cwd)
        if machines:
            sections["machines"] = machines
    except Exception as e:  # noqa: BLE001
        log.debug("operator_context: machines unavailable: %s", e)

    try:
        conn.close()
    except Exception:
        pass

    payload = _build_payload(sections, max_chars=max(200, int(max_chars)))
    # Tag the payload so the model knows it's the unified-context blob and not
    # an arbitrary tool result. Tag is single-character to keep it cheap.
    payload["_kind"] = "operator_context"
    if cwd:
        payload["_cwd"] = cwd
    return payload


__all__ = [
    "get_operator_context",
    # Exposed for tests:
    "_build_payload",
    "_PRIORITY",
]
