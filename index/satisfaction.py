"""Satisfaction-profile storage layer.

Records every (reaction, ai_behavior) pair observed in the session corpus so
the model can understand which of its behaviors tend to land well and which
tend to produce pushback.

Schema mirrors :mod:`index.voice`: idempotent ``ensure_schema``, plain
sqlite3, no third-party deps.

Tables
------
satisfaction_profile
    One row per ``(reaction, ai_behavior)`` cross-tab cell. ``count``
    accumulates across ingest runs; ``last_ts`` is the unix timestamp of the
    most recent observation.

satisfaction_meta
    Single-row summary: ``total_praise_count``, ``total_frustration_count``,
    ``sample_size`` (total reactions seen), ``updated_ts``.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

__all__ = [
    "SATISFACTION_SCHEMA",
    "ensure_schema",
    "upsert_satisfaction_pair",
    "persist_satisfaction",
    "get_satisfaction_summary",
]


SATISFACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS satisfaction_profile (
    reaction     TEXT NOT NULL,
    ai_behavior  TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    last_ts      INTEGER,
    PRIMARY KEY(reaction, ai_behavior)
);

CREATE TABLE IF NOT EXISTS satisfaction_meta (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    total_praise_count      INTEGER NOT NULL DEFAULT 0,
    total_frustration_count INTEGER NOT NULL DEFAULT 0,
    sample_size             INTEGER NOT NULL DEFAULT 0,
    updated_ts              INTEGER
);
"""

_PRAISE_REACTIONS = frozenset({
    "praise_quality",
    "praise_launch",
    "silent_accept",
})

_FRUSTRATION_REACTIONS = frozenset({
    "frustration_drift",
    "frustration_broke",
    "frustration_wtf",
    "frustration_scope",
    "frustration_verbosity",
})


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create satisfaction tables if they do not exist (idempotent)."""
    conn.executescript(SATISFACTION_SCHEMA)


def upsert_satisfaction_pair(
    conn: sqlite3.Connection,
    reaction: str,
    ai_behavior: str,
    delta_count: int = 1,
    last_ts: int | None = None,
) -> None:
    """Accumulate ``delta_count`` for the given ``(reaction, ai_behavior)`` cell."""
    ensure_schema(conn)
    ts = int(last_ts if last_ts is not None else time.time())
    conn.execute(
        """
        INSERT INTO satisfaction_profile(reaction, ai_behavior, count, last_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(reaction, ai_behavior) DO UPDATE SET
            count   = count + excluded.count,
            last_ts = MAX(last_ts, excluded.last_ts)
        """,
        (reaction, ai_behavior, delta_count, ts),
    )


def persist_satisfaction(
    conn: sqlite3.Connection,
    profile_dict: dict[str, Any],
) -> None:
    """Bulk-upsert from the dict produced by :func:`extractors.satisfaction.extract_satisfaction`.

    Expected shape::

        {
            "matrix": {reaction: {ai_behavior: count}},
            "sample_size": N,
            "total_praise_count": P,
            "total_frustration_count": F,
            "updated_ts": T,   # optional
        }
    """
    ensure_schema(conn)
    ts = int(profile_dict.get("updated_ts") or time.time())
    matrix: dict[str, dict[str, int]] = profile_dict.get("matrix", {})
    for reaction, behaviors in matrix.items():
        for ai_behavior, count in behaviors.items():
            if count:
                upsert_satisfaction_pair(conn, reaction, ai_behavior, count, ts)

    # Upsert meta row (id=1 always).
    conn.execute(
        """
        INSERT INTO satisfaction_meta(id, total_praise_count, total_frustration_count,
                                      sample_size, updated_ts)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            total_praise_count      = excluded.total_praise_count,
            total_frustration_count = excluded.total_frustration_count,
            sample_size             = excluded.sample_size,
            updated_ts              = excluded.updated_ts
        """,
        (
            profile_dict.get("total_praise_count", 0),
            profile_dict.get("total_frustration_count", 0),
            profile_dict.get("sample_size", 0),
            ts,
        ),
    )


def get_satisfaction_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the satisfaction cross-tab plus derived top picks.

    Returns
    -------
    dict with keys:
        matrix
            ``{reaction: {ai_behavior: count}}``
        top_praise_behavior
            The ``ai_behavior`` most often followed by a praise reaction,
            or ``None`` if there are no praise observations.
        top_frustration_behavior
            The ``ai_behavior`` most often followed by a frustration reaction,
            or ``None`` if there are no frustration observations.
        sample_size
            Total reactions observed (from meta row; 0 if table is empty).
        total_praise_count
            Total praise reactions.
        total_frustration_count
            Total frustration reactions.
    """
    ensure_schema(conn)

    rows = conn.execute(
        "SELECT reaction, ai_behavior, count FROM satisfaction_profile"
    ).fetchall()

    matrix: dict[str, dict[str, int]] = {}
    praise_totals: dict[str, int] = {}
    frustration_totals: dict[str, int] = {}

    for row in rows:
        try:
            reaction = row["reaction"]
            ai_behavior = row["ai_behavior"]
            count = row["count"]
        except (IndexError, KeyError, TypeError):
            reaction, ai_behavior, count = row[0], row[1], row[2]

        matrix.setdefault(reaction, {})[ai_behavior] = count

        if reaction in _PRAISE_REACTIONS:
            praise_totals[ai_behavior] = praise_totals.get(ai_behavior, 0) + count
        elif reaction in _FRUSTRATION_REACTIONS:
            frustration_totals[ai_behavior] = frustration_totals.get(ai_behavior, 0) + count

    top_praise = max(praise_totals, key=lambda k: praise_totals[k]) if praise_totals else None
    top_frustration = (
        max(frustration_totals, key=lambda k: frustration_totals[k])
        if frustration_totals
        else None
    )

    meta_row = conn.execute(
        "SELECT total_praise_count, total_frustration_count, sample_size "
        "FROM satisfaction_meta WHERE id = 1"
    ).fetchone()

    if meta_row is not None:
        try:
            total_praise = meta_row["total_praise_count"]
            total_frustration = meta_row["total_frustration_count"]
            sample_size = meta_row["sample_size"]
        except (IndexError, KeyError, TypeError):
            total_praise, total_frustration, sample_size = meta_row[0], meta_row[1], meta_row[2]
    else:
        total_praise = sum(praise_totals.values())
        total_frustration = sum(frustration_totals.values())
        sample_size = total_praise + total_frustration

    return {
        "matrix": matrix,
        "top_praise_behavior": top_praise,
        "top_frustration_behavior": top_frustration,
        "sample_size": sample_size,
        "total_praise_count": total_praise,
        "total_frustration_count": total_frustration,
    }
