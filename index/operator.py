"""Operator-profile storage layer.

A tiny key/value table — one row per profile field — that holds the single
operator's identity (name, handles, machines, vendor preferences,
philosophy). Append-friendly: each extractor pass can ``upsert`` only the
fields it has new evidence for, leaving the rest of the row set untouched.

The table is intentionally not part of the main ``schema.sql`` so the
``operator_profile`` extractor can live behind a feature flag while still
sharing :mod:`index.db.connect`. :func:`ensure_schema` is idempotent and is
called automatically by every public function in this module.

Each ``value`` is JSON-encoded so we can store strings, lists and dicts in
the same column without growing the schema every time we discover a new
field. ``confidence`` is a 0..1 float; ``sources`` is a JSON list of
``"source_file:line"`` citations the extractor accumulated.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable

__all__ = [
    "OPERATOR_PROFILE_SCHEMA",
    "ensure_schema",
    "upsert_profile_field",
    "get_profile",
    "get_profile_field",
]


OPERATOR_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_profile (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,            -- JSON-encoded scalar / list / dict
    confidence  REAL DEFAULT 0.5,
    sources     TEXT,                     -- JSON list of "source_file:line"
    updated_ts  INTEGER
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``operator_profile`` table if it does not exist.

    Safe to call from any code path; the connection's autocommit state is
    preserved (no explicit BEGIN/COMMIT here — matches the rest of the
    ``index/`` package which relies on ``isolation_level=None``).
    """
    conn.execute(OPERATOR_PROFILE_SCHEMA)


def upsert_profile_field(
    conn: sqlite3.Connection,
    key: str,
    value: Any,
    confidence: float = 0.5,
    sources: Iterable[str] | None = None,
) -> None:
    """Insert or update a single profile field.

    ``value`` is JSON-encoded before storage, so the caller can pass a str,
    int, list or dict transparently. ``sources`` is normalised to a list of
    strings; ``None`` is stored as ``"[]"``.
    """
    ensure_schema(conn)

    src_list: list[str]
    if sources is None:
        src_list = []
    else:
        src_list = [str(s) for s in sources]

    value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
    sources_json = json.dumps(src_list, ensure_ascii=False)
    now = int(time.time())

    # Clamp confidence to [0.0, 1.0] — match the rest of the codebase's
    # defensive clamping (see Extraction.__post_init__).
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    conn.execute(
        """
        INSERT INTO operator_profile(key, value, confidence, sources, updated_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value      = excluded.value,
            confidence = excluded.confidence,
            sources    = excluded.sources,
            updated_ts = excluded.updated_ts
        """,
        (key, value_json, conf, sources_json, now),
    )


def _decode_row(row: sqlite3.Row | tuple) -> tuple[Any, float, list[str]]:
    """Decode a stored row back into (value, confidence, sources)."""
    try:
        raw_value = row["value"]
        conf = row["confidence"]
        raw_sources = row["sources"]
    except (IndexError, KeyError, TypeError):
        raw_value = row[1]
        conf = row[2]
        raw_sources = row[3]

    try:
        value = json.loads(raw_value) if raw_value is not None else None
    except (TypeError, json.JSONDecodeError):
        value = raw_value

    sources: list[str]
    if raw_sources is None:
        sources = []
    else:
        try:
            decoded = json.loads(raw_sources)
            sources = list(decoded) if isinstance(decoded, list) else []
        except (TypeError, json.JSONDecodeError):
            sources = []

    return value, float(conf if conf is not None else 0.5), sources


def get_profile_field(
    conn: sqlite3.Connection, key: str
) -> tuple[Any, float, list[str]] | None:
    """Return ``(value, confidence, sources)`` for ``key`` or ``None``."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT key, value, confidence, sources, updated_ts "
        "FROM operator_profile WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return _decode_row(row)


def get_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the full profile as a flat dict of ``key -> value``.

    Confidence and sources are exposed as parallel sub-dicts under the
    reserved keys ``"_confidence"`` and ``"_sources"`` so callers that just
    want the values can ignore them, while introspection-y callers (like
    the MCP tool) can still surface provenance.

    Empty DB → ``{"_confidence": {}, "_sources": {}}``.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT key, value, confidence, sources, updated_ts "
        "FROM operator_profile ORDER BY key"
    ).fetchall()

    values: dict[str, Any] = {}
    confidences: dict[str, float] = {}
    sources_by_key: dict[str, list[str]] = {}
    for row in rows:
        try:
            key = row["key"]
        except (IndexError, KeyError, TypeError):
            key = row[0]
        value, conf, srcs = _decode_row(row)
        values[key] = value
        confidences[key] = conf
        sources_by_key[key] = srcs

    values["_confidence"] = confidences
    values["_sources"] = sources_by_key
    return values
