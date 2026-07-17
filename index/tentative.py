"""``tentative_facts`` — staging table for single-mention claims.

When :func:`index.decay.resolve_conflict` returns ``"tentative"`` the new
claim doesn't yet have enough evidence (or quality) to overturn the
canonical row, but we don't want to drop the signal either. We park it
here, bump ``evidence_count`` on every re-mention, and let
:func:`promote_eligible` graduate it once it crosses a threshold *and*
has been seen across a wide enough time window (so a single chatty
session can't promote itself).

Also covers the migration helper :func:`add_supersession_columns`, which
idempotently adds the ``superseded_at`` / ``superseded_by_id`` /
``evidence_count`` columns to existing profile tables (operator_profile,
standing_decisions, bans). The columns are nullable / defaulted so
existing rows keep working.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

__all__ = [
    "SCHEMA_SQL",
    "ensure_schema",
    "add_tentative",
    "promote_eligible",
    "drop_expired",
    "add_supersession_columns",
]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tentative_facts (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,                  -- e.g. "email_primary", "default_provider"
  value TEXT NOT NULL,                -- JSON
  source_kind TEXT,
  source_uuid TEXT,
  evidence_count INTEGER DEFAULT 1,
  first_seen_ts INTEGER NOT NULL,
  last_seen_ts INTEGER NOT NULL,
  promoted_at INTEGER,                -- null = still tentative
  dropped_at INTEGER,                 -- null = active candidate
  UNIQUE(key, value)
);
CREATE INDEX IF NOT EXISTS idx_tentative_key ON tentative_facts(key, last_seen_ts DESC);
"""

# Minimum spread between first_seen and last_seen for a tentative fact to be
# eligible for promotion. Defaults to 1 hour — short enough that a multi-hour
# session repeating a claim qualifies, long enough that a single burst of
# duplicated text in one tool result doesn't auto-promote.
DEFAULT_MIN_SPREAD_S = 3600


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``tentative_facts`` if it does not exist. Idempotent."""
    conn.executescript(SCHEMA_SQL)


def add_tentative(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    source_kind: str,
    source_uuid: str,
    ts: int,
) -> None:
    """Insert or bump ``evidence_count`` on a tentative fact.

    Dedup is on ``(key, value)`` — a re-mention of the same value bumps the
    count and updates ``last_seen_ts`` while leaving ``first_seen_ts``
    untouched. ``source_kind`` / ``source_uuid`` are refreshed to point at
    the most recent mention so a downstream promotion has a usable
    citation.
    """
    ensure_schema(conn)
    ts_int = int(ts)
    conn.execute(
        """
        INSERT INTO tentative_facts
            (key, value, source_kind, source_uuid,
             evidence_count, first_seen_ts, last_seen_ts)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(key, value) DO UPDATE SET
            evidence_count = tentative_facts.evidence_count + 1,
            last_seen_ts   = excluded.last_seen_ts,
            source_kind    = excluded.source_kind,
            source_uuid    = excluded.source_uuid
        """,
        (key, value, source_kind, source_uuid, ts_int, ts_int),
    )


def promote_eligible(
    conn: sqlite3.Connection,
    threshold: int = 2,
    ttl_days: int = 30,
    min_spread_s: int = DEFAULT_MIN_SPREAD_S,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Find tentative facts ready to graduate.

    A fact is *eligible* when:
      * ``evidence_count >= threshold``
      * ``last_seen_ts - first_seen_ts >= min_spread_s`` — i.e. it was
        observed across at least one non-trivial time gap, not all in one
        tight burst.
      * ``promoted_at`` and ``dropped_at`` are both NULL (still live).

    Returns the eligible rows as dicts. Side effect: each returned row is
    marked ``promoted_at = now`` so a subsequent call won't re-emit it.

    Also drops expired candidates (see :func:`drop_expired`) so a single
    call to ``promote_eligible`` keeps the staging table tidy.
    """
    ensure_schema(conn)
    now = int(now_ts) if now_ts is not None else int(time.time())

    # Sweep expired first so the row counts in the returned list are accurate.
    drop_expired(conn, ttl_days=ttl_days, now_ts=now)

    rows = conn.execute(
        """
        SELECT id, key, value, source_kind, source_uuid,
               evidence_count, first_seen_ts, last_seen_ts
        FROM tentative_facts
        WHERE promoted_at IS NULL
          AND dropped_at  IS NULL
          AND evidence_count >= ?
          AND (last_seen_ts - first_seen_ts) >= ?
        ORDER BY evidence_count DESC, last_seen_ts DESC
        """,
        (int(threshold), int(min_spread_s)),
    ).fetchall()

    promoted: list[dict[str, Any]] = []
    for row in rows:
        try:
            rid = row["id"]
            promoted.append(
                {
                    "id": row["id"],
                    "key": row["key"],
                    "value": row["value"],
                    "source_kind": row["source_kind"],
                    "source_uuid": row["source_uuid"],
                    "evidence_count": row["evidence_count"],
                    "first_seen_ts": row["first_seen_ts"],
                    "last_seen_ts": row["last_seen_ts"],
                }
            )
        except (IndexError, KeyError, TypeError):
            rid = row[0]
            promoted.append(
                {
                    "id": row[0],
                    "key": row[1],
                    "value": row[2],
                    "source_kind": row[3],
                    "source_uuid": row[4],
                    "evidence_count": row[5],
                    "first_seen_ts": row[6],
                    "last_seen_ts": row[7],
                }
            )
        conn.execute(
            "UPDATE tentative_facts SET promoted_at = ? WHERE id = ?",
            (now, rid),
        )

    return promoted


def drop_expired(
    conn: sqlite3.Connection,
    ttl_days: int = 30,
    now_ts: int | None = None,
) -> int:
    """Mark tentative facts older than ``ttl_days`` as dropped.

    A row is expired when its ``last_seen_ts`` is older than ``ttl_days``
    ago *and* it hasn't already been promoted or dropped. Returns the
    number of rows marked.
    """
    ensure_schema(conn)
    now = int(now_ts) if now_ts is not None else int(time.time())
    cutoff = now - int(ttl_days) * 86400
    cur = conn.execute(
        """
        UPDATE tentative_facts
        SET    dropped_at = ?
        WHERE  promoted_at IS NULL
          AND  dropped_at  IS NULL
          AND  last_seen_ts < ?
        """,
        (now, cutoff),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Migration helper for the canonical profile tables
# ---------------------------------------------------------------------------


# Tables that participate in the append-supersede policy. The migration is
# defensive: tables that don't exist yet are skipped (their owning module's
# ``ensure_schema`` will create them lazily, and a future call here will pick
# up the columns).
_SUPERSEDE_TABLES = ("operator_profile", "standing_decisions", "bans")

_SUPERSEDE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("superseded_at", "INTEGER"),
    ("superseded_by_id", "INTEGER"),
    ("evidence_count", "INTEGER DEFAULT 1"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()
    cols: set[str] = set()
    for row in rows:
        try:
            cols.add(row["name"])
        except (IndexError, KeyError, TypeError):
            cols.add(row[1])
    return cols


def add_supersession_columns(
    conn: sqlite3.Connection,
    tables: tuple[str, ...] = _SUPERSEDE_TABLES,
) -> dict[str, list[str]]:
    """Idempotently add supersession columns to the given profile tables.

    Returns a ``{table: [added_columns...]}`` map for inspection. Tables
    that don't exist are skipped silently — callers that need the table
    materialised first should call its module's ``ensure_schema`` before
    invoking this.
    """
    added: dict[str, list[str]] = {}
    for table in tables:
        existing = _columns(conn, table)
        if not existing:
            continue  # table absent; nothing to migrate
        added[table] = []
        for col, decl in _SUPERSEDE_COLUMNS:
            if col in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                added[table].append(col)
            except sqlite3.OperationalError:
                # Race or unsupported column; leave it for next migration pass.
                continue
    return added
