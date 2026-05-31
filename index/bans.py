"""Storage layer for ``bans`` + ``failed_attempts``.

Two small SQLite tables that back the
:mod:`mcp_server.extras.bans_tools` MCP surface. Kept out of the main
``schema.sql`` because the bans extractor is opt-in (it ships as part of
this slice) — the schema is created on first write via
:func:`ensure_schema` and is cheap to call repeatedly.

The dedup contract:

* ``bans`` is keyed by ``(banned_thing, category, context_clause)`` so each
  reassertion of the same ban (e.g. "never use <provider>" in three different
  sessions) bumps ``reassertion_count`` and ``last_reasserted_ts`` instead
  of inserting a duplicate row.

* ``failed_attempts`` is keyed by ``(attempt, cwd)`` so the same failed
  approach is only stored once per project context.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

__all__ = [
    "BANS_SCHEMA",
    "FAILED_ATTEMPTS_SCHEMA",
    "ensure_schema",
    "upsert_ban",
    "upsert_failed_attempt",
    "check_banned",
    "list_failed_attempts",
]

log = logging.getLogger(__name__)


BANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bans (
    id INTEGER PRIMARY KEY,
    banned_thing TEXT NOT NULL,
    category TEXT,
    ban_strength TEXT NOT NULL,
    ban_text TEXT NOT NULL,
    context_clause TEXT,
    first_banned_ts INTEGER,
    last_reasserted_ts INTEGER,
    reassertion_count INTEGER DEFAULT 1,
    source_session TEXT,
    UNIQUE(banned_thing, category, context_clause)
);
CREATE INDEX IF NOT EXISTS idx_bans_thing ON bans(banned_thing);
"""

FAILED_ATTEMPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS failed_attempts (
    id INTEGER PRIMARY KEY,
    attempt TEXT NOT NULL,
    replaced_by TEXT,
    reason TEXT,
    cwd TEXT,
    attempted_ts INTEGER,
    abandoned_ts INTEGER,
    UNIQUE(attempt, cwd)
);
CREATE INDEX IF NOT EXISTS idx_failed_attempts_attempt ON failed_attempts(attempt);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create both tables. Safe to call on every write."""
    conn.executescript(BANS_SCHEMA + FAILED_ATTEMPTS_SCHEMA)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if *name* is a table in the connected DB.

    Read paths use this instead of :func:`ensure_schema` — the MCP server opens
    the index ``mode=ro``, so a CREATE TABLE (even ``IF NOT EXISTS``) raises
    ``attempt to write a readonly database``. A read against a not-yet-created
    table simply means "no bans / no failed attempts recorded".
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_unix(ts: Any) -> int:
    """Best-effort conversion of int/float/datetime/str → unix seconds."""
    if ts is None:
        return int(time.time())
    if isinstance(ts, (int, float)):
        return int(ts)
    try:
        from datetime import datetime

        if isinstance(ts, datetime):
            return int(ts.timestamp())
    except Exception:
        pass
    # Last resort: now.
    return int(time.time())


def _normalize_thing(thing: str) -> str:
    return (thing or "").strip().lower()


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def upsert_ban(
    conn: sqlite3.Connection,
    *,
    banned_thing: str,
    ban_strength: str,
    ban_text: str,
    category: str | None = None,
    context_clause: str | None = None,
    ts: Any = None,
    source_session: str | None = None,
) -> None:
    """Insert a ban or reassert an existing one.

    The first write for a given ``(banned_thing, category, context_clause)``
    sets ``first_banned_ts`` and ``reassertion_count=1``. Subsequent writes
    bump ``reassertion_count`` and update ``last_reasserted_ts`` — the
    ``ban_text`` / ``ban_strength`` get refreshed too so the most recent
    verbatim quote wins (later operator language is presumed more current).
    """
    ensure_schema(conn)
    now = _to_unix(ts)
    thing = _normalize_thing(banned_thing)

    # SQLite UNIQUE treats NULLs as distinct, which would defeat dedup when
    # ``context_clause`` / ``category`` is absent. Normalize to an empty
    # string for the uniqueness key so absolute bans collapse to one row.
    clause_key = context_clause or ""
    category_key = category or ""

    # Manual upsert: SELECT-then-UPDATE-or-INSERT. We can't rely on
    # ``ON CONFLICT(banned_thing, category, context_clause)`` because the
    # UNIQUE index treats NULL as distinct in SQLite — even with empty-string
    # sentinels the ON CONFLICT path was unreliable across SQLite versions.
    row = conn.execute(
        """
        SELECT id, reassertion_count, first_banned_ts FROM bans
         WHERE banned_thing = ?
           AND COALESCE(category, '') = ?
           AND COALESCE(context_clause, '') = ?
        """,
        (thing, category_key, clause_key),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO bans(
                banned_thing, category, ban_strength, ban_text,
                context_clause, first_banned_ts, last_reasserted_ts,
                reassertion_count, source_session
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                thing,
                category,
                ban_strength,
                ban_text,
                clause_key or None,
                now,
                now,
                source_session,
            ),
        )
    else:
        try:
            ban_id = row["id"]
            new_count = (row["reassertion_count"] or 1) + 1
        except (IndexError, KeyError, TypeError):
            ban_id = row[0]
            new_count = (row[1] or 1) + 1
        conn.execute(
            """
            UPDATE bans
               SET ban_strength       = ?,
                   ban_text           = ?,
                   last_reasserted_ts = ?,
                   reassertion_count  = ?,
                   source_session     = ?
             WHERE id = ?
            """,
            (
                ban_strength,
                ban_text,
                now,
                new_count,
                source_session,
                ban_id,
            ),
        )


def upsert_failed_attempt(
    conn: sqlite3.Connection,
    *,
    attempt: str,
    replaced_by: str | None = None,
    reason: str | None = None,
    cwd: str | None = None,
    attempted_ts: Any = None,
    abandoned_ts: Any = None,
) -> None:
    """Insert or refresh a failed-attempt row.

    UNIQUE is ``(attempt, cwd)`` — same approach tried in two different
    projects is two rows. Updates refresh ``replaced_by`` / ``reason`` /
    ``abandoned_ts`` (the newest information is usually the most accurate).
    """
    ensure_schema(conn)
    a = (attempt or "").strip().lower()
    cwd_key = cwd or ""
    aban = _to_unix(abandoned_ts) if abandoned_ts is not None else _to_unix(attempted_ts)
    att = _to_unix(attempted_ts)

    conn.execute(
        """
        INSERT INTO failed_attempts(
            attempt, replaced_by, reason, cwd, attempted_ts, abandoned_ts
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(attempt, cwd) DO UPDATE SET
            replaced_by   = COALESCE(excluded.replaced_by, failed_attempts.replaced_by),
            reason        = COALESCE(excluded.reason,      failed_attempts.reason),
            abandoned_ts  = excluded.abandoned_ts
        """,
        (a, replaced_by, reason, cwd_key or None, att, aban),
    )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def check_banned(conn: sqlite3.Connection, thing: str) -> dict | None:
    """Return the strongest matching ban for ``thing`` or ``None``.

    "Strongest" precedence: ``absolute > context > preference``. Within the
    same strength tier the most recently reasserted row wins.
    """
    ensure_schema(conn)
    norm = _normalize_thing(thing)
    if not norm:
        return None
    rows = conn.execute(
        """
        SELECT banned_thing, category, ban_strength, ban_text,
               context_clause, first_banned_ts, last_reasserted_ts,
               reassertion_count, source_session
          FROM bans
         WHERE banned_thing = ?
        """,
        (norm,),
    ).fetchall()
    if not rows:
        return None

    # Convert rows to dicts and pick the strongest.
    strength_rank = {"absolute": 3, "context": 2, "preference": 1}

    def _key(r: sqlite3.Row) -> tuple:
        return (
            strength_rank.get(r["ban_strength"], 0),
            r["last_reasserted_ts"] or 0,
        )

    best = max(rows, key=_key)
    return {
        "banned_thing": best["banned_thing"],
        "category": best["category"],
        "ban_strength": best["ban_strength"],
        "ban_text": best["ban_text"],
        "context_clause": best["context_clause"],
        "first_banned_ts": best["first_banned_ts"],
        "last_reasserted_ts": best["last_reasserted_ts"],
        "reassertion_count": best["reassertion_count"],
        "source_session": best["source_session"],
    }


def list_failed_attempts(
    conn: sqlite3.Connection,
    topic: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Return failed-attempt rows, newest first, optionally filtered by topic.

    ``topic`` is matched as a case-insensitive substring on either
    ``attempt`` or ``reason`` (operators often phrase the same idea two ways).
    """
    ensure_schema(conn)
    if topic:
        pat = f"%{topic.lower()}%"
        rows = conn.execute(
            """
            SELECT attempt, replaced_by, reason, cwd, attempted_ts, abandoned_ts
              FROM failed_attempts
             WHERE LOWER(attempt) LIKE ?
                OR LOWER(COALESCE(reason, '')) LIKE ?
             ORDER BY COALESCE(abandoned_ts, attempted_ts) DESC
             LIMIT ?
            """,
            (pat, pat, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT attempt, replaced_by, reason, cwd, attempted_ts, abandoned_ts
              FROM failed_attempts
             ORDER BY COALESCE(abandoned_ts, attempted_ts) DESC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]
