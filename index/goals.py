"""Per-project goal stack — schema, write API, queries.

The goal stack is a small auxiliary table that tracks *what the operator is
trying to do on this project*, with a status state machine
(``active → paused → abandoned``, ``active → blocked → active``,
``active → done``). The MCP layer surfaces ``get_active_goal()`` at
SessionStart so a new session can lead with "Last in-flight: <goal>.
Continue?" instead of asking the operator to re-state context.

This module is the one place that owns the ``goal_stack`` schema, the upsert
path that folds ``goal`` and ``goal_progress`` extractions into it, the
state-machine transitions, and the read queries the MCP tools call.

Design notes:

* The table is keyed on ``UNIQUE(project, goal_text)`` so re-ingesting the
  same session twice is idempotent and a goal restated in a later session
  collapses to the same row (with ``last_progress_ts`` bumped).
* ``status`` transitions are computed by :func:`recompute_statuses`, which
  is meant to be called once per ingest pass (so a long-paused goal
  appears as ``paused`` even if no new records mention it). Tests use a
  ``now_ts`` parameter to drive a clock fixture.
* ``project`` stores the worktree-collapsed project root (see
  :func:`index.paths.project_key`). Normalization happens *here*: writes
  via :func:`upsert_from_extractions` and reads via
  :func:`get_active_goal` / :func:`list_goals` all pass ``cwd`` through
  ``project_key`` so worktree checkouts pool under their owning repo.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from index.paths import project_key

log = logging.getLogger(__name__)


__all__ = [
    "GOAL_STACK_SCHEMA",
    "GoalRow",
    "apply_schema",
    "upsert_from_extractions",
    "recompute_statuses",
    "get_active_goal",
    "get_active_goal_for_cwd",
    "list_goals",
    # Constants exposed for tests + callers.
    "PAUSED_DAYS",
    "ABANDONED_DAYS",
    "VALID_STATUSES",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


GOAL_STACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_stack (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  goal_text TEXT NOT NULL,
  declared_ts INTEGER NOT NULL,
  last_progress_ts INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  related_projects TEXT,
  source_session TEXT,
  UNIQUE(project, goal_text)
);
CREATE INDEX IF NOT EXISTS idx_goals_project_status ON goal_stack(project, status);
CREATE INDEX IF NOT EXISTS idx_goals_last_progress ON goal_stack(last_progress_ts DESC);
"""


VALID_STATUSES = ("active", "paused", "blocked", "done", "abandoned")

_DAY = 86_400  # seconds
PAUSED_DAYS = 30
ABANDONED_DAYS = 60


_PROJECT_KEY_CASE = (
    "CASE "
    "WHEN instr(project, '/.claude/worktrees/') > 0 "
    "THEN substr(project, 1, instr(project, '/.claude/worktrees/') - 1) "
    "WHEN instr(project, '/.worktrees/') > 0 "
    "THEN substr(project, 1, instr(project, '/.worktrees/') - 1) "
    "ELSE project END"
)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create ``goal_stack`` + its indexes if not present. Idempotent.

    Also runs a one-time, idempotent backfill that collapses any legacy
    worktree ``project`` values to their owning repo root so reads pool
    correctly. The UPDATE is deterministic, so re-running is a no-op once
    the projects are already canonical.
    """
    conn.executescript(GOAL_STACK_SCHEMA)
    # One-time, idempotent backfill: collapse any legacy worktree ``project``
    # to its owning repo root. Guarded by a cheap existence probe so the common
    # case (already-canonical rows) takes no write lock at all — important
    # because this runs on every open and a blanket UPDATE would otherwise
    # contend with concurrent readers/writers on the same DB file.
    try:
        needs = conn.execute(
            f"SELECT 1 FROM goal_stack WHERE project <> ({_PROJECT_KEY_CASE}) LIMIT 1"
        ).fetchone()
        if needs is not None:
            conn.execute(
                f"UPDATE goal_stack SET project = {_PROJECT_KEY_CASE} "
                f"WHERE project <> ({_PROJECT_KEY_CASE})"
            )
    except sqlite3.OperationalError:
        # Table may not exist yet on a brand-new conn that bypassed the
        # executescript above; the CREATE is idempotent so this is defensive.
        pass


# ---------------------------------------------------------------------------
# Read API surface
# ---------------------------------------------------------------------------


@dataclass
class GoalRow:
    """Plain dataclass form of a ``goal_stack`` row."""

    id: int
    project: str
    goal_text: str
    declared_ts: int
    last_progress_ts: int | None
    status: str
    related_projects: list[str] = field(default_factory=list)
    source_session: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> GoalRow:
        if hasattr(row, "keys"):
            d = dict(row)
        else:
            cols = (
                "id",
                "project",
                "goal_text",
                "declared_ts",
                "last_progress_ts",
                "status",
                "related_projects",
                "source_session",
            )
            d = dict(zip(cols, row, strict=False))
        related_raw = d.get("related_projects")
        related: list[str] = []
        if related_raw:
            try:
                parsed = json.loads(related_raw)
                if isinstance(parsed, list):
                    related = [str(x) for x in parsed]
            except (TypeError, ValueError):
                related = []
        return cls(
            id=int(d["id"]),
            project=str(d["project"]),
            goal_text=str(d["goal_text"]),
            declared_ts=int(d["declared_ts"]),
            last_progress_ts=(
                int(d["last_progress_ts"]) if d.get("last_progress_ts") is not None else None
            ),
            status=str(d["status"]),
            related_projects=related,
            source_session=d.get("source_session"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "goal_text": self.goal_text,
            "declared_ts": self.declared_ts,
            "last_progress_ts": self.last_progress_ts,
            "status": self.status,
            "related_projects": list(self.related_projects),
            "source_session": self.source_session,
        }


# ---------------------------------------------------------------------------
# Upsert path — fold extractions into goal_stack
# ---------------------------------------------------------------------------


def _ts_int(ts: Any) -> int | None:
    """Coerce a datetime / int / float / ISO string into a unix int.

    Returns ``None`` if the value can't be coerced — the caller will skip
    the row rather than insert a goal with a missing declared_ts.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if hasattr(ts, "timestamp"):
        try:
            return int(ts.timestamp())
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(ts, str):
        # Best-effort ISO parse; we don't pull in dateutil for this.
        from datetime import datetime

        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def upsert_from_extractions(
    conn: sqlite3.Connection,
    extractions: Iterable[Any],
) -> tuple[int, int]:
    """Fold ``goal`` + ``goal_progress`` extractions into ``goal_stack``.

    Returns ``(goals_upserted, progress_applied)``. Idempotent: re-running
    on the same extractions touches the same rows without duplicating.

    Each extraction is expected to expose ``kind``, ``content``,
    ``session_id``, ``cwd``, ``ts``, ``context`` (any mapping-like). Both
    ``Extraction`` dataclasses and the post-pipeline ``replace(...)``ed
    copies satisfy this; tests may pass ``SimpleNamespace`` shapes.
    """
    goals_upserted = 0
    progress_applied = 0

    for ext in extractions:
        kind = getattr(ext, "kind", None)
        if kind not in ("goal", "goal_progress"):
            continue
        cwd = project_key(getattr(ext, "cwd", None))
        if not cwd:
            continue
        ts = _ts_int(getattr(ext, "ts", None))
        if ts is None:
            continue
        session_id = getattr(ext, "session_id", None) or ""
        content = getattr(ext, "content", None)
        if not isinstance(content, str) or not content.strip():
            continue

        if kind == "goal":
            ctx = getattr(ext, "context", None) or {}
            status_hint = None
            if isinstance(ctx, dict):
                status_hint = ctx.get("status_hint")
            # INSERT OR IGNORE first, then UPDATE the timestamps so an
            # existing row preserves its declared_ts but bumps progress.
            conn.execute(
                """
                INSERT OR IGNORE INTO goal_stack(
                    project, goal_text, declared_ts, last_progress_ts,
                    status, source_session
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cwd, content, ts, ts, "active", session_id),
            )
            # If status_hint says "done"/"blocked", apply on insert-or-existing.
            if status_hint in ("done", "blocked"):
                conn.execute(
                    """
                    UPDATE goal_stack
                       SET status = ?,
                           last_progress_ts = MAX(COALESCE(last_progress_ts, 0), ?)
                     WHERE project = ? AND goal_text = ?
                    """,
                    (status_hint, ts, cwd, content),
                )
            else:
                # Bump last_progress_ts if this declaration is newer than
                # whatever's recorded (a restated goal counts as a touch).
                conn.execute(
                    """
                    UPDATE goal_stack
                       SET last_progress_ts = MAX(COALESCE(last_progress_ts, 0), ?)
                     WHERE project = ? AND goal_text = ?
                    """,
                    (ts, cwd, content),
                )
            goals_upserted += 1
            continue

        # kind == "goal_progress"
        # Bump last_progress_ts on every goal in this project that's
        # currently active or blocked. Progress is project-scoped — we
        # don't try to text-match the progress marker against the goal
        # text; that would be brittle and most operators only juggle
        # 1–3 active goals per project anyway.
        cur = conn.execute(
            """
            UPDATE goal_stack
               SET last_progress_ts = MAX(COALESCE(last_progress_ts, 0), ?)
             WHERE project = ?
               AND status IN ('active', 'blocked')
            """,
            (ts, cwd),
        )
        if cur.rowcount and cur.rowcount > 0:
            progress_applied += cur.rowcount

    return goals_upserted, progress_applied


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def recompute_statuses(
    conn: sqlite3.Connection,
    now_ts: int | None = None,
) -> dict[str, int]:
    """Run the per-row status transitions. Returns counts by transition.

    Rules (mirrors the implementation rubric):

    * ``active``  → ``paused``    if no progress in PAUSED_DAYS (30d)
                                  AND status is not already terminal.
    * ``active``  → ``abandoned`` if no activity in ABANDONED_DAYS (60d)
                                  AND no completion. We treat "no activity"
                                  as ``last_progress_ts`` (or ``declared_ts``
                                  if null) older than ABANDONED_DAYS.
    * ``paused``  → ``abandoned`` same threshold.
    * ``blocked`` → ``active``    if progress in last PAUSED_DAYS — i.e.
                                  ``last_progress_ts`` is fresh again.
    * ``done`` / ``abandoned``    are terminal — never re-opened here.

    A caller bumping a goal back to ``blocked`` is expected to do so via
    the upsert path's ``status_hint`` channel, not through this function.
    """
    if now_ts is None:
        now_ts = int(time.time())

    counts = {
        "active_to_paused": 0,
        "active_to_abandoned": 0,
        "paused_to_abandoned": 0,
        "blocked_to_active": 0,
    }

    paused_cutoff = now_ts - PAUSED_DAYS * _DAY
    abandoned_cutoff = now_ts - ABANDONED_DAYS * _DAY

    # active → abandoned (60d, never completed). Do BEFORE paused so we
    # don't shuttle through 'paused' on the way to 'abandoned'.
    cur = conn.execute(
        """
        UPDATE goal_stack
           SET status = 'abandoned'
         WHERE status = 'active'
           AND COALESCE(last_progress_ts, declared_ts) < ?
        """,
        (abandoned_cutoff,),
    )
    counts["active_to_abandoned"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # active → paused (30d, not yet abandoned).
    cur = conn.execute(
        """
        UPDATE goal_stack
           SET status = 'paused'
         WHERE status = 'active'
           AND COALESCE(last_progress_ts, declared_ts) < ?
        """,
        (paused_cutoff,),
    )
    counts["active_to_paused"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # paused → abandoned (60d).
    cur = conn.execute(
        """
        UPDATE goal_stack
           SET status = 'abandoned'
         WHERE status = 'paused'
           AND COALESCE(last_progress_ts, declared_ts) < ?
        """,
        (abandoned_cutoff,),
    )
    counts["paused_to_abandoned"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # blocked → active (progress in last PAUSED_DAYS).
    cur = conn.execute(
        """
        UPDATE goal_stack
           SET status = 'active'
         WHERE status = 'blocked'
           AND last_progress_ts IS NOT NULL
           AND last_progress_ts >= ?
        """,
        (paused_cutoff,),
    )
    counts["blocked_to_active"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    return counts


# ---------------------------------------------------------------------------
# Read queries
# ---------------------------------------------------------------------------


def get_active_goal(
    conn: sqlite3.Connection,
    project: str,
) -> GoalRow | None:
    """Return the most-recently-progressed active goal for ``project``.

    "Active" here includes ``blocked`` because a blocked goal is still
    "in flight" from the operator's perspective — they want to see it
    surfaced at SessionStart even more than a quietly-progressing one.

    Returns ``None`` if there's no active or blocked goal in the project.
    """
    row = conn.execute(
        """
        SELECT id, project, goal_text, declared_ts, last_progress_ts,
               status, related_projects, source_session
          FROM goal_stack
         WHERE project = ?
           AND status IN ('active', 'blocked')
         ORDER BY COALESCE(last_progress_ts, declared_ts) DESC,
                  declared_ts DESC
         LIMIT 1
        """,
        (project_key(project),),
    ).fetchone()
    if row is None:
        return None
    return GoalRow.from_row(row)


def get_active_goal_for_cwd(
    conn: sqlite3.Connection,
    cwd: str | None,
) -> GoalRow | None:
    """Active goal for ``cwd``, pooling worktree checkouts to their repo root.

    Thin wrapper over :func:`get_active_goal` that maps a (possibly worktree)
    ``cwd`` to its owning project via :func:`index.paths.project_key`. Returns
    ``None`` when ``cwd`` is ``None`` or no active/blocked goal exists.
    """
    if cwd is None:
        return None
    pkey = project_key(cwd)
    if pkey is None:
        return None
    return get_active_goal(conn, pkey)


def list_goals(
    conn: sqlite3.Connection,
    project: str | None = None,
    status: str = "active",
    limit: int = 10,
) -> list[GoalRow]:
    """List goals filtered by project + status.

    ``status="any"`` returns every status; otherwise filters to a single
    value (raises on unknown). When ``project`` is None, returns goals
    across all projects (useful for cross-project "what's open everywhere"
    surfacing in the slash-command layer).
    """
    if status != "any" and status not in VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected 'any' or one of {VALID_STATUSES}")

    sql = (
        "SELECT id, project, goal_text, declared_ts, last_progress_ts, "
        "status, related_projects, source_session FROM goal_stack WHERE 1=1"
    )
    params: list[Any] = []
    if project is not None:
        sql += " AND project = ?"
        params.append(project_key(project))
    if status != "any":
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY COALESCE(last_progress_ts, declared_ts) DESC, declared_ts DESC LIMIT ?"
    params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    return [GoalRow.from_row(r) for r in rows]
