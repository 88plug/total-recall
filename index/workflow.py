"""Workflow-profile storage layer.

A key/value table — one row per measured workflow field — mirroring the
voice_profile table design. Values are JSON-encoded so the schema stays
stable as new fields are added to :class:`extractors.workflow.WorkflowProfile`.

Table: ``workflow_profile``
  key          TEXT PRIMARY KEY
  value        TEXT NOT NULL    -- JSON-encoded scalar / list / dict
  updated_ts   INTEGER          -- Unix seconds of last write

Public API mirrors :mod:`index.voice`:
  :func:`ensure_schema`         — idempotent CREATE IF NOT EXISTS
  :func:`persist_workflow`      — bulk-upsert from a WorkflowProfile
  :func:`get_workflow`          — read back as a plain dict
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from extractors.workflow import WorkflowProfile

__all__ = [
    "WORKFLOW_PROFILE_SCHEMA",
    "ensure_schema",
    "persist_workflow",
    "get_workflow",
]


WORKFLOW_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_profile (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,   -- JSON-encoded scalar / list / dict
    updated_ts INTEGER          -- Unix seconds
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``workflow_profile`` table if it does not exist."""
    conn.execute(WORKFLOW_PROFILE_SCHEMA)


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode(raw: Any) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def persist_workflow(
    conn: sqlite3.Connection,
    profile: WorkflowProfile | dict,
    updated_ts: int | None = None,
) -> None:
    """Bulk-upsert all fields from ``profile`` into the ``workflow_profile`` table.

    Accepts either a :class:`~extractors.workflow.WorkflowProfile` dataclass or a
    plain dict (e.g. as returned by :func:`get_workflow` or by
    ``extract_workflow_incremental``). Dict keys prefixed with ``_`` (sidecar
    metadata such as ``_updated_ts``) are silently skipped.

    ``updated_ts`` defaults to ``int(time.time())``. The caller is responsible
    for committing the connection.
    """
    ensure_schema(conn)
    ts = int(updated_ts if updated_ts is not None else time.time())
    if isinstance(profile, dict):
        data = {k: v for k, v in profile.items() if not k.startswith("_")}
    else:
        data = asdict(profile)
    for key, value in data.items():
        conn.execute(
            """
            INSERT INTO workflow_profile(key, value, updated_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                updated_ts = excluded.updated_ts
            """,
            (key, _encode(value), ts),
        )


def get_workflow(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the full workflow profile as a flat dict ``key -> value``.

    An ``_updated_ts`` sidecar dict (``{key -> unix_seconds}``) is included
    so callers know how fresh each field is.

    Empty table → ``{"_updated_ts": {}}``.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT key, value, updated_ts FROM workflow_profile ORDER BY key"
    ).fetchall()

    values: dict[str, Any] = {}
    updated_ts: dict[str, Any] = {}

    for row in rows:
        try:
            key = row["key"]
            raw_value = row["value"]
            uts = row["updated_ts"]
        except (IndexError, KeyError, TypeError):
            key, raw_value, uts = row[0], row[1], row[2]
        values[key] = _decode(raw_value)
        updated_ts[key] = uts

    values["_updated_ts"] = updated_ts
    return values
