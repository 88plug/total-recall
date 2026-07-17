"""MCP tools for the per-project goal stack.

These tools wrap :mod:`index.goals` so the model can surface the operator's
most-recently-progressed active goal at SessionStart and list the broader
goal stack on demand.

Sandbox: read-only against the recall index DB. Tools degrade to a clear
``error`` payload when the DB isn't initialized or when ``index.goals`` is
absent on this branch (defensive import — mirrors how
:mod:`mcp_server.tools` handles missing siblings).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive import of the goals index module.
# ---------------------------------------------------------------------------


def _load_goals_module():
    """Return ``index.goals`` or ``None`` with a logged warning."""
    try:
        from index import goals  # type: ignore
    except Exception as e:  # ImportError or downstream blow-up
        log.warning("index.goals unavailable: %s", e)
        return None
    return goals


def _current_cwd() -> str:
    """Best-effort: the cwd Claude Code launched in.

    Mirrors :func:`mcp_server.tools._current_cwd` so we don't take a hard
    dependency on that module's helpers (it might be partially loaded).
    """
    import os
    return os.environ.get("PWD") or os.getcwd()


def _index_missing_error() -> dict:
    return {
        "error": "index not initialized; run `total-recall index` to build it",
        "db_path": str(DB_PATH),
    }


def _goals_module_missing_error() -> list[dict]:
    return [{"error": "index.goals module not available on this branch"}]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(title="Get Active Goal", annotations=ToolAnnotations(readOnlyHint=True))
def get_active_goal(cwd: str | None = None) -> dict | None:
    (
        "Return the most-recently-progressed active goal for a project. Use at session start to "
        "lead with 'Last in-flight: <goal>. Continue?' instead of asking the operator to "
        "re-state context. Returns {project, goal_text, declared_ts, last_progress_ts, status, "
        "source_session} or None if no active/blocked goal exists for the project. ``cwd`` "
        "defaults to the current Claude Code cwd."
    )
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    goals_mod = _load_goals_module()
    if goals_mod is None:
        return {"error": "index.goals module not available on this branch"}

    target = cwd or _current_cwd()
    try:
        row = goals_mod.get_active_goal(conn, target)
    except sqlite3.OperationalError as e:
        # goal_stack table may not exist yet on a freshly-ingested DB that
        # hasn't had the goals schema applied. Degrade rather than raise.
        log.debug("get_active_goal: %s", e)
        return None
    except Exception as e:
        log.exception("get_active_goal failed")
        return {"error": f"get_active_goal failed: {e!r}"}
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    if row is None:
        return None
    return row.to_dict()


@mcp.tool(title="List Goals", annotations=ToolAnnotations(readOnlyHint=True))
def list_goals(
    cwd: str | None = None,
    status: str = "active",
    limit: int = 10,
) -> list[dict]:
    (
        "List goals by project + status. Use when the operator asks what they've been working "
        "on or when surveying the goal stack. ``status`` is one of "
        "active|paused|blocked|done|abandoned|any. ``cwd`` defaults to the current Claude Code "
        "cwd; pass an empty string to list across ALL projects. Returns up to ``limit`` rows "
        "{project, goal_text, declared_ts, last_progress_ts, status, ...} ordered by "
        "most-recently-progressed first."
    )
    conn = get_conn()
    if conn is None:
        return [_index_missing_error()]

    goals_mod = _load_goals_module()
    if goals_mod is None:
        return _goals_module_missing_error()

    # Empty string opts out of cwd filtering (cross-project listing).
    if cwd is None:
        project: str | None = _current_cwd()
    elif cwd == "":
        project = None
    else:
        project = cwd

    try:
        rows = goals_mod.list_goals(
            conn, project=project, status=status, limit=int(limit)
        )
    except ValueError as e:
        return [{"error": str(e)}]
    except sqlite3.OperationalError as e:
        log.debug("list_goals: %s", e)
        return []
    except Exception as e:
        log.exception("list_goals failed")
        return [{"error": f"list_goals failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    return [r.to_dict() for r in rows]


__all__ = ["get_active_goal", "list_goals"]
