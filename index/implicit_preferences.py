"""Implicit-preferences storage layer.

Owns:

* ``CREATE TABLE IF NOT EXISTS implicit_preferences`` (idempotent),
* :func:`persist_implicit_preferences` — bulk-upsert a profile,
* :func:`get_implicit_preferences` — read-query used by the MCP tool.

The table stores one row per ``(category, value)`` pair. Timestamps are
unix seconds (INTEGER). ``sample_phrases_json`` is a JSON-encoded list of
up to 3 short strings. The table is created lazily via :func:`ensure_schema`
so any caller holding an arbitrary connection can bootstrap it without
needing a hard schema-migration coupling.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from extractors.implicit_preferences import ImplicitPreference, ImplicitPreferenceProfile

__all__ = [
    "SCHEMA_SQL",
    "ensure_schema",
    "persist_implicit_preferences",
    "get_implicit_preferences",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS implicit_preferences (
    id                  INTEGER PRIMARY KEY,
    category            TEXT    NOT NULL,
    value               TEXT    NOT NULL,
    confidence          REAL    NOT NULL DEFAULT 0.0,
    evidence_sessions   INTEGER NOT NULL DEFAULT 0,
    evidence_projects   INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    sample_phrases_json TEXT    NOT NULL DEFAULT '[]',
    first_seen_ts       INTEGER,
    last_seen_ts        INTEGER,
    updated_ts          INTEGER,
    UNIQUE(category, value)
);
CREATE INDEX IF NOT EXISTS idx_implicit_prefs_category
    ON implicit_preferences(category);
CREATE INDEX IF NOT EXISTS idx_implicit_prefs_confidence
    ON implicit_preferences(confidence DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the ``implicit_preferences`` table + indexes."""
    conn.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def persist_implicit_preferences(
    conn: sqlite3.Connection,
    profile: ImplicitPreferenceProfile,
    *,
    now: int | None = None,
) -> None:
    """Upsert every preference in ``profile`` into the database.

    On conflict (same ``category`` + ``value``), the row is updated:
    evidence counts are replaced (re-evaluated from a fresh extraction pass),
    and ``updated_ts`` is refreshed. ``first_seen_ts`` is only set once —
    it is never overwritten on subsequent upserts so the stability clock
    keeps ticking from the true first observation.

    ``now`` defaults to ``int(time.time())`` and is accepted as a parameter
    to make tests deterministic.
    """
    ensure_schema(conn)
    ts = int(now if now is not None else time.time())

    for pref in profile.preferences:
        phrases_json = json.dumps(
            pref.sample_phrases[:3], ensure_ascii=False
        )
        conn.execute(
            """
            INSERT INTO implicit_preferences
                (category, value, confidence, evidence_sessions,
                 evidence_projects, contradiction_count,
                 sample_phrases_json, first_seen_ts, last_seen_ts, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, value) DO UPDATE SET
                confidence          = excluded.confidence,
                evidence_sessions   = excluded.evidence_sessions,
                evidence_projects   = excluded.evidence_projects,
                contradiction_count = excluded.contradiction_count,
                sample_phrases_json = excluded.sample_phrases_json,
                first_seen_ts       = COALESCE(
                                          implicit_preferences.first_seen_ts,
                                          excluded.first_seen_ts
                                      ),
                last_seen_ts        = excluded.last_seen_ts,
                updated_ts          = excluded.updated_ts
            """,
            (
                pref.category,
                pref.value,
                round(float(pref.confidence), 6),
                int(pref.evidence_sessions),
                int(pref.evidence_projects),
                int(pref.contradiction_count),
                phrases_json,
                ts,  # first_seen_ts (only used on INSERT; COALESCE protects on UPDATE)
                ts,  # last_seen_ts
                ts,  # updated_ts
            ),
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_implicit_preferences(
    conn: sqlite3.Connection,
    *,
    min_confidence: float = 0.6,
    category: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return implicit preferences above ``min_confidence``, ordered by confidence desc.

    Each row is returned as a plain dict. ``sample_phrases_json`` is decoded
    back to a Python list in the output (key becomes ``sample_phrases``).

    Optional ``category`` filter restricts to a single category
    (``"edit_strategy"``, ``"shell_command"``, ``"format"``, ``"vocabulary"``).
    """
    ensure_schema(conn)
    clauses: list[str] = ["confidence >= ?"]
    params: list[Any] = [float(min_confidence)]
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    where = "WHERE " + " AND ".join(clauses)
    params.append(int(limit))

    rows = conn.execute(
        f"""
        SELECT id, category, value, confidence, evidence_sessions,
               evidence_projects, contradiction_count, sample_phrases_json,
               first_seen_ts, last_seen_ts, updated_ts
          FROM implicit_preferences
         {where}
         ORDER BY confidence DESC, evidence_sessions DESC
         LIMIT ?
        """,
        params,
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            d: dict[str, Any] = {k: row[k] for k in row.keys()}
        except (TypeError, AttributeError):
            keys = [
                "id", "category", "value", "confidence", "evidence_sessions",
                "evidence_projects", "contradiction_count", "sample_phrases_json",
                "first_seen_ts", "last_seen_ts", "updated_ts",
            ]
            d = dict(zip(keys, row))
        raw_phrases = d.pop("sample_phrases_json", "[]")
        try:
            d["sample_phrases"] = json.loads(raw_phrases) if raw_phrases else []
        except (TypeError, json.JSONDecodeError):
            d["sample_phrases"] = []
        result.append(d)
    return result
