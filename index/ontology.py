"""Ontology storage layer — projects, machines, vocabulary.

This module owns three tightly-coupled tables that together encode the
operator's *domain ontology*:

* ``projects``    — every directory the operator has used Claude Code in,
  with derived metadata (purpose, primary language, last-active timestamp).
* ``machines``    — hostnames the operator references across sessions
  (workstations, relays, Proxmox hosts) plus LAN / Tailscale / GPU data.
* ``vocabulary``  — operator-specific terms with definitions, so the MCP
  layer can answer "what does X mean in this operator's world".

The schema is intentionally separate from the main ``index/schema.sql`` so
the ontology extractor can be feature-flagged independently. Every public
function in this module calls :func:`ensure_schema` first; the table
``CREATE``s are all ``IF NOT EXISTS`` so callers can re-invoke freely.

We mirror the pattern from :mod:`index.operator`: short, defensive,
upsert-friendly. ``related_projects`` is JSON-encoded so adjacency lists
can grow without altering the schema.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

__all__ = [
    "ONTOLOGY_SCHEMA",
    "ensure_schema",
    "upsert_project",
    "upsert_machine",
    "upsert_vocabulary_term",
    "bump_vocabulary_frequency",
    "get_project",
    "get_machine",
    "get_term",
    "list_projects",
    "list_machines",
    "machines_for_cwd",
    "list_terms",
]


ONTOLOGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    cwd               TEXT PRIMARY KEY,
    display_name      TEXT,
    purpose           TEXT,
    primary_language  TEXT,
    related_projects  TEXT,                          -- JSON list of cwds
    first_seen_ts     INTEGER,
    last_active_ts    INTEGER,
    message_count     INTEGER
);

CREATE TABLE IF NOT EXISTS machines (
    hostname      TEXT PRIMARY KEY,
    role          TEXT,
    lan_ip        TEXT,
    tailscale_ip  TEXT,
    public_ip     TEXT,
    gpu           TEXT,
    os            TEXT,
    notes         TEXT,
    last_seen_ts  INTEGER
);

CREATE TABLE IF NOT EXISTS vocabulary (
    term           TEXT PRIMARY KEY,
    definition     TEXT NOT NULL,
    category       TEXT,
    frequency      INTEGER DEFAULT 1,
    first_seen_ts  INTEGER
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the three ontology tables if missing. Idempotent."""
    conn.executescript(ONTOLOGY_SCHEMA)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def upsert_project(
    conn: sqlite3.Connection,
    cwd: str,
    *,
    display_name: str | None = None,
    purpose: str | None = None,
    primary_language: str | None = None,
    related_projects: Iterable[str] | None = None,
    first_seen_ts: int | None = None,
    last_active_ts: int | None = None,
    message_count: int | None = None,
) -> None:
    """Insert or update a project row.

    ``None`` for any non-key field means "don't overwrite" — we coalesce
    against the existing column so an extractor pass that only learned the
    purpose doesn't blow away a previously-mined message_count.
    """
    ensure_schema(conn)
    related_json: str | None
    if related_projects is None:
        related_json = None
    else:
        related_json = json.dumps(
            sorted({str(r) for r in related_projects if r}), ensure_ascii=False
        )

    conn.execute(
        """
        INSERT INTO projects(
            cwd, display_name, purpose, primary_language,
            related_projects, first_seen_ts, last_active_ts, message_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cwd) DO UPDATE SET
            display_name     = COALESCE(excluded.display_name, projects.display_name),
            purpose          = COALESCE(excluded.purpose, projects.purpose),
            primary_language = COALESCE(excluded.primary_language, projects.primary_language),
            related_projects = COALESCE(excluded.related_projects, projects.related_projects),
            first_seen_ts    = COALESCE(
                MIN(excluded.first_seen_ts, projects.first_seen_ts),
                projects.first_seen_ts, excluded.first_seen_ts),
            last_active_ts   = COALESCE(
                MAX(excluded.last_active_ts, projects.last_active_ts),
                projects.last_active_ts, excluded.last_active_ts),
            message_count    = COALESCE(excluded.message_count, projects.message_count)
        """,
        (
            cwd,
            display_name,
            purpose,
            primary_language,
            related_json,
            first_seen_ts,
            last_active_ts,
            message_count,
        ),
    )


def _row_to_project(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        d = dict(row)
    except (AttributeError, TypeError):
        cols = [
            "cwd",
            "display_name",
            "purpose",
            "primary_language",
            "related_projects",
            "first_seen_ts",
            "last_active_ts",
            "message_count",
        ]
        d = dict(zip(cols, row, strict=False))
    raw = d.get("related_projects")
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
            d["related_projects"] = list(decoded) if isinstance(decoded, list) else []
        except (TypeError, json.JSONDecodeError):
            d["related_projects"] = []
    else:
        d["related_projects"] = []
    return d


def get_project(conn: sqlite3.Connection, cwd: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM projects WHERE cwd = ?", (cwd,)).fetchone()
    return _row_to_project(row)


def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM projects ORDER BY last_active_ts DESC NULLS LAST, cwd"
    ).fetchall()
    return [_row_to_project(r) for r in rows if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------


def upsert_machine(
    conn: sqlite3.Connection,
    hostname: str,
    *,
    role: str | None = None,
    lan_ip: str | None = None,
    tailscale_ip: str | None = None,
    public_ip: str | None = None,
    gpu: str | None = None,
    os: str | None = None,
    notes: str | None = None,
    last_seen_ts: int | None = None,
) -> None:
    """Insert / update a machine row, coalescing nulls onto existing data."""
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO machines(
            hostname, role, lan_ip, tailscale_ip, public_ip,
            gpu, os, notes, last_seen_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hostname) DO UPDATE SET
            role         = COALESCE(excluded.role, machines.role),
            lan_ip       = COALESCE(excluded.lan_ip, machines.lan_ip),
            tailscale_ip = COALESCE(excluded.tailscale_ip, machines.tailscale_ip),
            public_ip    = COALESCE(excluded.public_ip, machines.public_ip),
            gpu          = COALESCE(excluded.gpu, machines.gpu),
            os           = COALESCE(excluded.os, machines.os),
            notes        = COALESCE(excluded.notes, machines.notes),
            last_seen_ts = COALESCE(
                MAX(excluded.last_seen_ts, machines.last_seen_ts),
                machines.last_seen_ts, excluded.last_seen_ts)
        """,
        (
            hostname,
            role,
            lan_ip,
            tailscale_ip,
            public_ip,
            gpu,
            os,
            notes,
            last_seen_ts,
        ),
    )


def _row_to_machine(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        return dict(row)
    except (AttributeError, TypeError):
        cols = [
            "hostname",
            "role",
            "lan_ip",
            "tailscale_ip",
            "public_ip",
            "gpu",
            "os",
            "notes",
            "last_seen_ts",
        ]
        return dict(zip(cols, row, strict=False))


def get_machine(conn: sqlite3.Connection, hostname: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM machines WHERE hostname = ?", (hostname,)).fetchone()
    return _row_to_machine(row)


def list_machines(
    conn: sqlite3.Connection, name_pattern: str | None = None
) -> list[dict[str, Any]]:
    """Return every machine row, optionally filtered by a regex over hostname.

    The filter is applied in Python (SQLite has REGEXP only when a callback
    is registered; we'd rather keep this module dependency-free).
    """
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM machines ORDER BY hostname").fetchall()
    out = [_row_to_machine(r) for r in rows if r is not None]
    if name_pattern:
        import re

        try:
            pat = re.compile(name_pattern)
        except re.error:
            return out  # invalid regex — degrade to "no filter"
        out = [m for m in out if m and pat.search(m.get("hostname", ""))]
    return out  # type: ignore[return-value]


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


def machines_for_cwd(
    conn: sqlite3.Connection,
    cwd: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Most-recently-seen machines, newest first.

    The ``machines`` table is not scoped to a ``cwd`` — the machine inventory
    is operator-global. ``cwd`` is accepted for call-site symmetry with the
    other operator-context collectors but ignored. Ordered by ``last_seen_ts``
    descending. Read-only safe: an absent table yields ``[]``.
    """
    if not _table_exists(conn, "machines"):
        return []
    rows = conn.execute(
        """
        SELECT * FROM machines
         ORDER BY COALESCE(last_seen_ts, 0) DESC, hostname ASC
         LIMIT ?
        """,
        (max(0, int(limit)),),
    ).fetchall()
    return [m for m in (_row_to_machine(r) for r in rows) if m is not None]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def upsert_vocabulary_term(
    conn: sqlite3.Connection,
    term: str,
    definition: str,
    *,
    category: str | None = None,
    frequency: int | None = None,
    first_seen_ts: int | None = None,
) -> None:
    """Insert or update a vocabulary term.

    Unlike ``bump_vocabulary_frequency``, this overwrites ``definition`` and
    ``category`` — use it when you have an authoritative definition (e.g.
    hardcoded seed data); use ``bump_vocabulary_frequency`` when you're
    just incrementing observed counts.
    """
    ensure_schema(conn)
    now = int(time.time())
    seen = first_seen_ts if first_seen_ts is not None else now
    freq = frequency if frequency is not None else 1
    conn.execute(
        """
        INSERT INTO vocabulary(term, definition, category, frequency, first_seen_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(term) DO UPDATE SET
            definition    = excluded.definition,
            category      = COALESCE(excluded.category, vocabulary.category),
            frequency     = COALESCE(excluded.frequency, vocabulary.frequency),
            first_seen_ts = COALESCE(
                MIN(excluded.first_seen_ts, vocabulary.first_seen_ts),
                vocabulary.first_seen_ts, excluded.first_seen_ts)
        """,
        (term, definition, category, freq, seen),
    )


def bump_vocabulary_frequency(
    conn: sqlite3.Connection, term: str, *, by: int = 1
) -> None:
    """Increment ``frequency`` by ``by`` for an existing term.

    No-op if the term is unknown — frequencies are only meaningful once a
    definition exists (we don't want to grow the table with bare counts).
    """
    ensure_schema(conn)
    conn.execute(
        "UPDATE vocabulary SET frequency = frequency + ? WHERE term = ?",
        (by, term),
    )


def _row_to_term(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        return dict(row)
    except (AttributeError, TypeError):
        cols = ["term", "definition", "category", "frequency", "first_seen_ts"]
        return dict(zip(cols, row, strict=False))


def get_term(conn: sqlite3.Connection, term: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    # Match case-insensitively so callers can pass 'Relay Fleet' or 'relay fleet'.
    row = conn.execute(
        "SELECT * FROM vocabulary WHERE LOWER(term) = LOWER(?)", (term,)
    ).fetchone()
    return _row_to_term(row)


def list_terms(
    conn: sqlite3.Connection, category: str | None = None
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    if category:
        rows = conn.execute(
            "SELECT * FROM vocabulary WHERE category = ? ORDER BY frequency DESC, term",
            (category,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM vocabulary ORDER BY frequency DESC, term"
        ).fetchall()
    return [_row_to_term(r) for r in rows if r is not None]  # type: ignore[return-value]
