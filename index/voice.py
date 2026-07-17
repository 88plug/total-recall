"""Voice-profile storage layer.

A tiny key/value table — one row per measured voice field — that holds the
operator's communication cadence (casing, sentence length distribution,
profanity rate, signature typos, top first-words). Designed in the same
spirit as :mod:`index.operator`: append-friendly, JSON-encoded values, so
the schema doesn't need to grow when a new stat is added.

The table is kept separate from the main ``schema.sql`` so the
voice extractor can ship behind a feature flag while still sharing
:mod:`index.db.connect`. :func:`ensure_schema` is idempotent and is called
by every public function in this module.

Stored fields (set by :func:`extractors.voice_profile.measure_voice`):

* ``lowercase_start_pct`` — float, 0.0..1.0
* ``mean_chars`` / ``chars_p10`` / ``chars_p50`` / ``chars_p90`` — ints
* ``mean_tokens`` / ``tokens_p10`` / ``tokens_p50`` / ``tokens_p90`` — floats/ints
* ``ends_period_pct`` / ``ends_question_pct`` / ``ends_no_punct_pct`` — float 0..1
* ``imperative_first_word_pct`` — float 0..1
* ``signature_typos`` — JSON list of ``[typo, count]`` pairs
* ``top_first_words`` — JSON list of ``[word, count]`` pairs
* ``we_vs_i_ratio`` — float (``we+us+our`` count / ``i`` count)
* ``profanity_per_1k_turns`` — float
* ``one_word_turn_pct`` — float 0..1

``measured_at`` is unix seconds; ``sample_size`` is the number of natural
user turns the measurement was based on. Both are scalar columns rather
than JSON because every field shares the same provenance.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

__all__ = [
    "VOICE_PROFILE_SCHEMA",
    "ensure_schema",
    "upsert_voice_field",
    "get_voice",
    "get_voice_field",
    "persist_voice_profile",
]


VOICE_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_profile (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,            -- JSON-encoded scalar / list / dict
    measured_at  INTEGER,
    sample_size  INTEGER
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``voice_profile`` table if it does not exist."""
    conn.execute(VOICE_PROFILE_SCHEMA)


def upsert_voice_field(
    conn: sqlite3.Connection,
    key: str,
    value: Any,
    sample_size: int | None = None,
    measured_at: int | None = None,
) -> None:
    """Insert or update a single voice field.

    ``value`` is JSON-encoded so scalars, lists and dicts all round-trip
    without schema changes. ``measured_at`` defaults to ``int(time.time())``.
    """
    ensure_schema(conn)

    value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
    ts = int(measured_at if measured_at is not None else time.time())
    n: int | None
    try:
        n = int(sample_size) if sample_size is not None else None
    except (TypeError, ValueError):
        n = None

    conn.execute(
        """
        INSERT INTO voice_profile(key, value, measured_at, sample_size)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value       = excluded.value,
            measured_at = excluded.measured_at,
            sample_size = excluded.sample_size
        """,
        (key, value_json, ts, n),
    )


def _decode_value(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def get_voice_field(
    conn: sqlite3.Connection, key: str
) -> tuple[Any, int | None, int | None] | None:
    """Return ``(value, measured_at, sample_size)`` for ``key`` or ``None``."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT key, value, measured_at, sample_size FROM voice_profile WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        raw_value = row["value"]
        measured_at = row["measured_at"]
        sample_size = row["sample_size"]
    except (IndexError, KeyError, TypeError):
        raw_value, measured_at, sample_size = row[1], row[2], row[3]
    return _decode_value(raw_value), measured_at, sample_size


def get_voice(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the full voice profile as a flat dict ``key -> value``.

    Two sidecar dicts are added:

    * ``_measured_at`` — ``{key -> unix_seconds}``
    * ``_sample_size`` — ``{key -> int | None}``

    Empty table → ``{"_measured_at": {}, "_sample_size": {}}``.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT key, value, measured_at, sample_size FROM voice_profile ORDER BY key"
    ).fetchall()

    values: dict[str, Any] = {}
    measured_at: dict[str, int | None] = {}
    sample_size: dict[str, int | None] = {}
    for row in rows:
        try:
            key = row["key"]
            raw_value = row["value"]
            mat = row["measured_at"]
            ssz = row["sample_size"]
        except (IndexError, KeyError, TypeError):
            key, raw_value, mat, ssz = row[0], row[1], row[2], row[3]
        values[key] = _decode_value(raw_value)
        measured_at[key] = mat
        sample_size[key] = ssz

    values["_measured_at"] = measured_at
    values["_sample_size"] = sample_size
    return values


def persist_voice_profile(
    conn: sqlite3.Connection,
    fields: dict[str, Any],
    sample_size: int | None = None,
    measured_at: int | None = None,
) -> None:
    """Bulk-upsert every key in ``fields`` as a voice_profile row.

    Reserved keys ``_measured_at`` / ``_sample_size`` in the input dict are
    skipped so callers can pass the dict returned by :func:`get_voice` back
    through without polluting the table.
    """
    ensure_schema(conn)
    ts = int(measured_at if measured_at is not None else time.time())
    for key, value in fields.items():
        if key.startswith("_"):
            continue
        upsert_voice_field(conn, key, value, sample_size=sample_size, measured_at=ts)
