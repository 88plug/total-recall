"""MCP tools: pre-suggestion ban check + failed-attempts log.

Two read-only tools the model should consult **before** emitting a default
recommendation:

* :func:`check_banned` — "is X off-limits?" Returns the verbatim quote and
  the operator's strength label (absolute / context / preference) so the
  model can either avoid X outright or surface the context clause back to
  the user ("that provider is fine internally but you've said never to name
  it publicly — should I redact?").

* :func:`list_failed_attempts` — chronological list of approaches the
  operator already tried and abandoned. Use before suggesting something
  that "sounds reasonable" — odds are it's already on this list with a
  replaced_by alternative.

Both tools degrade gracefully when the index DB is missing (returns an
error dict instead of raising) so the MCP server can survive a
fresh-checkout / pre-first-index environment.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


def _load_bans_index():
    """Defensive import of :mod:`index.bans` (may not be merged on this branch)."""
    try:
        from index import bans as _bans  # type: ignore

        return _bans
    except Exception as e:  # pragma: no cover - import-time guard
        log.warning("index.bans unavailable: %s", e)
        return None


def _index_missing_error() -> dict:
    return {
        "error": "index not initialized; run `total-recall index` to build it",
        "db_path": str(DB_PATH),
    }


@mcp.tool()
def check_banned(thing: str) -> dict:
    """Check if a thing (provider, tool, library, framework, pattern) is banned by the operator. Returns {"banned": bool, "strength": ..., "verbatim_quote": ..., "context": ...}. Call BEFORE suggesting any default. Examples: check_banned("some-provider") → banned=absolute. check_banned("another-provider") → banned=context (only publicly)."""
    if not isinstance(thing, str) or not thing.strip():
        return {"banned": False, "error": "thing must be a non-empty string"}

    conn = get_conn()
    if conn is None:
        return {"banned": False, **_index_missing_error()}

    bans_mod = _load_bans_index()
    if bans_mod is None:
        return {
            "banned": False,
            "error": "index.bans module not available on this branch",
        }

    try:
        row = bans_mod.check_banned(conn, thing)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("check_banned() failed")
        return {"banned": False, "error": f"check_banned failed: {e!r}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if row is None:
        return {"banned": False, "thing": thing.lower()}

    return {
        "banned": True,
        "thing": row.get("banned_thing"),
        "strength": row.get("ban_strength"),
        "verbatim_quote": row.get("ban_text"),
        "context": row.get("context_clause"),
        "category": row.get("category"),
        "reassertion_count": row.get("reassertion_count"),
        "last_reasserted_ts": row.get("last_reasserted_ts"),
    }


@mcp.tool()
def list_failed_attempts(topic: str | None = None, limit: int = 10) -> list[dict]:
    """Failed approaches the operator has documented. Use before suggesting something that 'sounds reasonable' — it might already be on the failed-attempts log. Returns chronological list with replaced_by + reason."""
    conn = get_conn()
    if conn is None:
        return [_index_missing_error()]

    bans_mod = _load_bans_index()
    if bans_mod is None:
        return [{"error": "index.bans module not available on this branch"}]

    try:
        rows = bans_mod.list_failed_attempts(conn, topic=topic, limit=int(limit))
    except Exception as e:  # pragma: no cover - defensive
        log.exception("list_failed_attempts() failed")
        return [{"error": f"list_failed_attempts failed: {e!r}"}]
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "attempt": r.get("attempt"),
                "replaced_by": r.get("replaced_by"),
                "reason": r.get("reason"),
                "cwd": r.get("cwd"),
                "attempted_ts": r.get("attempted_ts"),
                "abandoned_ts": r.get("abandoned_ts"),
            }
        )
    return out


__all__ = ["check_banned", "list_failed_attempts"]


# Coerce unused-import warnings on Any — keeps the import for forward type use
# without making this module's surface noisier.
_ = Any
