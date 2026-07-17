"""Standing-decisions store — schema + UPSERT queries.

A *standing decision* is a position the operator has taken that should
outlive any single session ("provider-a > provider-b", "billing-x > billing-y",
"never use library-z"). The point is to keep future Claude from re-debating
something that's already been resolved. Reversed decisions still get kept
(with a money-burn annotation) so the next argument can be settled with
"we already learned that, $200 ago."

This module owns:

* CREATE-IF-NOT-EXISTS for the ``standing_decisions`` table + indexes,
* :func:`upsert_decision` — write/refresh a single row, idempotent on
  (topic, chose, scope), bumping ``assertion_count`` and
  ``last_reasserted_ts`` on every re-statement,
* :func:`mark_reversed` — flip a row to ``is_reversed=1`` and optionally
  attach the new winner + money burnt to learn the lesson,
* :func:`list_decisions` / :func:`get_for_topic` — read queries used by
  the MCP tools and the SessionStart surfacing.

The table is created lazily — callers that hold an arbitrary connection
(e.g. the MCP server holding an existing ``index.db``) just call
:func:`ensure_schema` once and proceed. We do NOT bake this into
``index/schema.sql`` because it lives on a separate worktree and we don't
want a hard migration coupling.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from index.paths import project_key

__all__ = [
    "SCHEMA_SQL",
    "ensure_schema",
    "upsert_decision",
    "mark_reversed",
    "list_decisions",
    "get_for_topic",
    "top_decisions_for_scope",
    "row_to_dict",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS standing_decisions (
  id INTEGER PRIMARY KEY,
  topic TEXT NOT NULL,
  chose TEXT NOT NULL,
  over TEXT,
  rationale TEXT,
  scope TEXT NOT NULL,
  first_asserted_ts INTEGER,
  last_reasserted_ts INTEGER,
  assertion_count INTEGER DEFAULT 1,
  is_reversed INTEGER DEFAULT 0,
  reversed_at_ts INTEGER,
  reversed_to TEXT,
  money_burn_usd REAL DEFAULT 0,
  source_session TEXT,
  UNIQUE(topic, chose, scope)
);
CREATE INDEX IF NOT EXISTS idx_decisions_topic ON standing_decisions(topic);
CREATE INDEX IF NOT EXISTS idx_decisions_count
    ON standing_decisions(assertion_count DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``standing_decisions`` table + indexes."""
    conn.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Coerce a sqlite3.Row into a plain dict (or ``None`` passthrough)."""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (AttributeError, TypeError):
        return dict(row)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def upsert_decision(
    conn: sqlite3.Connection,
    *,
    topic: str,
    chose: str,
    scope: str,
    over: str | None = None,
    rationale: str | None = None,
    ts: int | None = None,
    source_session: str | None = None,
    money_burn_usd: float = 0.0,
) -> int:
    """Insert or refresh a standing-decision row.

    Idempotent on ``(topic, chose, scope)``. On conflict:

    * ``assertion_count`` += 1
    * ``last_reasserted_ts`` = max(existing, ``ts``)
    * ``over`` / ``rationale`` / ``source_session`` filled in if previously
      null (never *replaced* once we have data — earlier rationale is
      usually the better one),
    * ``money_burn_usd`` is *added* to (it's a running total of what the
      operator burned learning the lesson).

    Returns the row id.
    """
    ts = ts if ts is not None else _now()
    ensure_schema(conn)

    # Fast path: try insert; on UNIQUE conflict do an UPDATE.
    cur = conn.execute(
        """
        INSERT INTO standing_decisions
            (topic, chose, over, rationale, scope,
             first_asserted_ts, last_reasserted_ts, assertion_count,
             money_burn_usd, source_session)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(topic, chose, scope) DO UPDATE SET
            assertion_count = standing_decisions.assertion_count + 1,
            last_reasserted_ts = MAX(
                COALESCE(standing_decisions.last_reasserted_ts, 0),
                excluded.last_reasserted_ts
            ),
            over = COALESCE(standing_decisions.over, excluded.over),
            rationale = COALESCE(standing_decisions.rationale, excluded.rationale),
            source_session = COALESCE(
                standing_decisions.source_session, excluded.source_session
            ),
            money_burn_usd =
                COALESCE(standing_decisions.money_burn_usd, 0)
                + COALESCE(excluded.money_burn_usd, 0)
        """,
        (
            topic,
            chose,
            over,
            rationale,
            scope,
            ts,
            ts,
            float(money_burn_usd or 0.0),
            source_session,
        ),
    )

    # Resolve the rowid (works for INSERT *and* UPDATE-on-conflict).
    if cur.lastrowid:
        # On UPDATE, lastrowid may still point at the existing row on most
        # SQLite versions, but be defensive and look it up by UNIQUE key.
        existing = conn.execute(
            "SELECT id FROM standing_decisions WHERE topic=? AND chose=? AND scope=?",
            (topic, chose, scope),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        return int(cur.lastrowid)
    row = conn.execute(
        "SELECT id FROM standing_decisions WHERE topic=? AND chose=? AND scope=?",
        (topic, chose, scope),
    ).fetchone()
    return int(row[0]) if row else -1


def mark_reversed(
    conn: sqlite3.Connection,
    *,
    topic: str,
    chose: str,
    scope: str,
    reversed_to: str | None = None,
    money_burn_usd: float | None = None,
    ts: int | None = None,
) -> bool:
    """Flag an existing decision as reversed.

    Returns ``True`` if a row was updated, ``False`` if no matching row
    exists (caller can decide whether to upsert+reverse in one shot).
    Money burn, when supplied, is *added* to any existing total.
    """
    ts = ts if ts is not None else _now()
    ensure_schema(conn)
    cur = conn.execute(
        """
        UPDATE standing_decisions
           SET is_reversed = 1,
               reversed_at_ts = ?,
               reversed_to = COALESCE(?, reversed_to),
               money_burn_usd =
                   COALESCE(money_burn_usd, 0) + COALESCE(?, 0)
         WHERE topic = ? AND chose = ? AND scope = ?
        """,
        (
            ts,
            reversed_to,
            float(money_burn_usd) if money_burn_usd is not None else None,
            topic,
            chose,
            scope,
        ),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_decisions(
    conn: sqlite3.Connection,
    *,
    topic: str | None = None,
    scope: str | None = None,
    include_reversed: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return standing decisions, ordered by assertion_count desc then ts desc.

    Filters: ``topic`` and ``scope`` are exact-match when supplied.
    ``include_reversed=False`` hides rows where ``is_reversed=1`` — useful
    when the caller only wants *active* positions.
    """
    ensure_schema(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if topic is not None:
        clauses.append("topic = ?")
        params.append(topic)
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    if not include_reversed:
        clauses.append("is_reversed = 0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT * FROM standing_decisions
        {where}
        ORDER BY assertion_count DESC,
                 COALESCE(last_reasserted_ts, 0) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [d for d in (row_to_dict(r) for r in rows) if d is not None]


def get_for_topic(
    conn: sqlite3.Connection,
    *,
    topic: str,
    scope: str | None = None,
) -> dict[str, Any] | None:
    """Single best decision for a topic (highest assertion_count, latest).

    When ``scope`` is supplied: prefer an exact-scope match; if none
    exists, fall back to a ``scope='global'`` row. Returns ``None`` if no
    row matches.
    """
    ensure_schema(conn)
    if scope is not None:
        row = conn.execute(
            """
            SELECT * FROM standing_decisions
             WHERE topic = ? AND scope = ?
             ORDER BY assertion_count DESC,
                      COALESCE(last_reasserted_ts, 0) DESC
             LIMIT 1
            """,
            (topic, scope),
        ).fetchone()
        if row is not None:
            return row_to_dict(row)
        # Fall through to global match.
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
        return row_to_dict(row)
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
    return row_to_dict(row)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if *name* is a table. Read paths use this instead of
    :func:`ensure_schema` so a mode=ro connection never trips
    ``attempt to write a readonly database``."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def top_decisions_for_scope(
    conn: sqlite3.Connection,
    cwd: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Top active standing decisions for ``cwd``'s project scope.

    Mirrors :func:`get_for_topic`'s cascade, applied at the list level: prefer
    rows whose ``scope`` equals the worktree-collapsed project root (see
    :func:`index.paths.project_key`); if that yields nothing, fall back to
    ``scope='global'`` rows; if still empty, return the strongest decisions of
    any scope. Reversed decisions are excluded. Ordered by ``assertion_count``
    then recency. Read-only safe (no schema write).
    """
    if not _table_exists(conn, "standing_decisions"):
        return []
    scope = project_key(cwd)

    def _query(where_extra: str, params: tuple) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT * FROM standing_decisions
             WHERE is_reversed = 0{where_extra}
             ORDER BY assertion_count DESC,
                      COALESCE(last_reasserted_ts, 0) DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        return [d for d in (row_to_dict(r) for r in rows) if d is not None]

    if scope is not None:
        rows = _query(" AND scope = ?", (scope, int(limit)))
        if rows:
            return rows
    rows = _query(" AND scope = 'global'", (int(limit),))
    if rows:
        return rows
    return _query("", (int(limit),))
