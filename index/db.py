"""Connection helpers for the total-recall SQLite index.

Single source of truth for:

* where the DB lives on disk (`DEFAULT_DB_PATH`),
* what PRAGMAs each connection needs (WAL, busy_timeout, foreign_keys),
* how the schema is applied (`apply_schema` — idempotent, reads
  ``schema.sql`` from this package).

Every other module in `index/` should open connections through
:func:`connect` instead of touching :mod:`sqlite3` directly so the PRAGMAs and
schema bootstrap are guaranteed.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

__all__ = [
    "DEFAULT_DB_PATH",
    "connect",
    "apply_schema",
    "vacuum",
]


def _default_db_path() -> Path:
    """Compute the on-disk DB path.

    Honors ``$CLAUDE_PLUGIN_DATA`` when set (Claude Code sets this for plugins).
    Falls back to ``~/.local/share/total-recall/`` (XDG-ish).
    """
    base_env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if base_env:
        base = Path(base_env).expanduser() / "total-recall"
    else:
        base = Path("~/.local/share/total-recall").expanduser()
    return base / "index.db"


DEFAULT_DB_PATH: Path = _default_db_path()

_SCHEMA_PATH: Path = Path(__file__).with_name("schema.sql")


def _ensure_parent_dir(db_path: Path) -> None:
    """Create the parent dir of ``db_path`` with 0700 perms if missing.

    0700 because session transcripts (and therefore everything derived from
    them) contain secrets, internal URLs, and private code. Don't leak.
    """
    parent = db_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Only tighten perms when we own the directory; ignore on weird FS.
    with contextlib.suppress(PermissionError, OSError):
        os.chmod(parent, 0o700)


def connect(
    db_path: Path = DEFAULT_DB_PATH,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open a connection to the total-recall index.

    * Creates parent dir (0700) if missing.
    * Sets WAL, busy_timeout=5000, foreign_keys=ON on every connection.
    * Applies the schema on first open (idempotent).
    * ``read_only=True`` opens the URI in ``mode=ro`` — schema apply is
      skipped and writes will fail (use for query-only consumers like the MCP
      server).
    """
    db_path = Path(db_path).expanduser()
    _ensure_parent_dir(db_path)

    if read_only:
        # URI mode lets us pass ?mode=ro without losing the WAL pragmas.
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    else:
        conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)

    conn.row_factory = sqlite3.Row
    # `busy_timeout` is set in milliseconds; `timeout=5.0` above is for the
    # Python wrapper but we also set the SQLite-level pragma to be explicit.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        # WAL is sticky to the file, but setting it cheaply per-connection is
        # harmless and guarantees the mode even on a fresh DB.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        apply_schema(conn)

    return conn


_CURRENT_SCHEMA_VERSION = "5"


def _read_schema_version(conn: sqlite3.Connection) -> str | None:
    """Return the recorded ``schema_meta.schema_version``, or ``None``.

    Returns ``None`` if either the ``schema_meta`` table is absent (fresh DB
    pre-bootstrap) or the row simply hasn't been inserted yet.
    """
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    # Support both sqlite3.Row and tuple-shaped rows.
    try:
        return str(row["value"])
    except (IndexError, KeyError, TypeError):
        return str(row[0])


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for ``table`` (empty if absent)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()
    cols: set[str] = set()
    for r in rows:
        try:
            cols.add(str(r["name"]))
        except (IndexError, KeyError, TypeError):
            cols.add(str(r[1]))
    return cols


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Idempotent v* → v4 migration.

    Adds ``source`` + ``dedup_superseded_by_source`` columns to ``messages``
    and ``extractions`` when missing. Existing rows are tagged as
    ``'claude_code'`` (the only source before XW8) so legacy data isn't
    silently re-classified as "unknown source".

    Safe to run repeatedly: each ALTER is guarded by a column-existence
    check via ``PRAGMA table_info``.
    """
    # messages.source / messages.dedup_superseded_by_source
    msg_cols = _table_columns(conn, "messages")
    if msg_cols:  # only migrate if the table exists
        if "source" not in msg_cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN source TEXT "
                "NOT NULL DEFAULT 'claude_code'"
            )
        if "dedup_superseded_by_source" not in msg_cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN dedup_superseded_by_source TEXT"
            )
        # The supporting index is created idempotently in schema.sql; if the
        # column was just added the CREATE INDEX in the executescript would
        # have errored before this point (sqlite executes left-to-right and
        # would parse the CREATE INDEX referencing the missing column). To
        # guarantee the index ends up present, re-create it here defensively.
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_source_cwd_ts "
                "ON messages(source, cwd, ts DESC)"
            )

    # extractions.source / extractions.dedup_superseded_by_source
    ext_cols = _table_columns(conn, "extractions")
    if ext_cols:
        if "source" not in ext_cols:
            conn.execute(
                "ALTER TABLE extractions ADD COLUMN source TEXT "
                "NOT NULL DEFAULT 'claude_code'"
            )
        if "dedup_superseded_by_source" not in ext_cols:
            conn.execute(
                "ALTER TABLE extractions ADD COLUMN dedup_superseded_by_source TEXT"
            )
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_extractions_source_kind "
                "ON extractions(source, kind, ts DESC)"
            )

    # Help the planner notice the new columns/index.
    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("ANALYZE")


# Backfill CASE: collapse worktree cwd to its owning repo root. Mirrors
# index.paths.project_key so the SQL backfill and the Python runtime agree.
_PROJECT_KEY_CASE = (
    "CASE "
    "WHEN instr(cwd, '/.claude/worktrees/') > 0 "
    "THEN substr(cwd, 1, instr(cwd, '/.claude/worktrees/') - 1) "
    "WHEN instr(cwd, '/.worktrees/') > 0 "
    "THEN substr(cwd, 1, instr(cwd, '/.worktrees/') - 1) "
    "ELSE cwd END"
)


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """Idempotent v* → v5 migration.

    Adds a ``project_key`` column to ``messages`` and ``extractions`` and
    backfills it by collapsing worktree ``cwd`` values to their owning repo
    root (see :func:`index.paths.project_key`). Safe to run repeatedly: each
    ALTER is guarded by a ``PRAGMA table_info`` check and the backfill UPDATE
    is deterministic, so a second run leaves the rows unchanged.
    """
    # Backfill is scoped to NULL rows only. Two reasons: (1) it keeps the
    # migration idempotent without re-touching already-canonical rows, and
    # (2) a blanket UPDATE on ``messages`` fires the ``messages_au`` FTS-sync
    # trigger for every row, which issues an FTS ``'delete'`` against the
    # contentless index for rows the index never saw (legacy rows inserted
    # before the triggers existed) — corrupting it. Restricting to NULL means
    # we touch each row exactly once, when it first gains the column.
    # The backfill UPDATE runs only on the ALTER (column just added). Re-runs
    # against an already-migrated DB skip it entirely — both because there is
    # nothing to do and because a blanket UPDATE on ``messages`` fires the
    # ``messages_au`` FTS-sync trigger, which is unsafe when the contentless
    # FTS index is out of sync with the base table (legacy rows the index
    # never saw). Gating on the ALTER keeps the write to exactly one pass.
    msg_cols = _table_columns(conn, "messages")
    if msg_cols and "project_key" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN project_key TEXT")
        conn.execute(f"UPDATE messages SET project_key = {_PROJECT_KEY_CASE}")
    if msg_cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_project_key_ts "
                "ON messages(project_key, ts DESC)"
            )

    ext_cols = _table_columns(conn, "extractions")
    if ext_cols and "project_key" not in ext_cols:
        conn.execute("ALTER TABLE extractions ADD COLUMN project_key TEXT")
        conn.execute(f"UPDATE extractions SET project_key = {_PROJECT_KEY_CASE}")
    if ext_cols:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_extractions_project_key_kind "
                "ON extractions(project_key, kind, ts DESC)"
            )

    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("ANALYZE")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Run ``schema.sql`` against ``conn``, then run any pending migrations.

    The script uses ``CREATE ... IF NOT EXISTS`` everywhere so this is safe to
    re-run on each open. After the script runs, the recorded
    ``schema_version`` is reconciled to the current version:

    * Absent / fresh DB → script just inserted ``'4'``; nothing to do.
    * Existing ``'1'`` DB → the v2 ``CREATE`` statements were no-ops for the
      v1 tables and *did* create the new v2 tables (turns / compactions /
      ingest_runs). We then run the v4 column migration and bump the version.
    * Existing ``'2'`` or ``'3'`` DB → ``messages`` / ``extractions`` already
      exist without the v4 ``source`` columns; the executescript above did
      not add columns to existing tables (CREATE TABLE IF NOT EXISTS is a
      no-op). :func:`_migrate_to_v4` adds them via ALTER TABLE.
    * Existing ``'4'`` DB → :func:`_migrate_to_v4` is idempotent; running it
      detects the columns are present and does nothing.
    """
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    # On pre-v4 DBs, the in-script ``CREATE INDEX idx_messages_source_cwd_ts``
    # references a column the ALTER TABLE migration hasn't added yet, so the
    # executescript will error halfway through. Detect that case and skip the
    # whole-script path; the migration plus a re-run will fill in the rest.
    pre_migrate = False
    msg_cols = _table_columns(conn, "messages")
    if msg_cols and ("source" not in msg_cols or "project_key" not in msg_cols):
        pre_migrate = True
    ext_cols = _table_columns(conn, "extractions")
    if ext_cols and ("source" not in ext_cols or "project_key" not in ext_cols):
        pre_migrate = True

    if pre_migrate:
        # Run the migrations FIRST so the columns exist, then re-execute the
        # full script so any new tables / indexes / triggers get created. The
        # in-script CREATE INDEX statements reference columns the migrations
        # add (``source``, ``project_key``), so they must precede the script.
        _migrate_to_v4(conn)
        _migrate_to_v5(conn)
        conn.executescript(sql)
    else:
        conn.executescript(sql)
        # Still call the migrations on a fresh DB — no-ops when columns
        # already exist, and they catch any partially-upgraded states.
        _migrate_to_v4(conn)
        _migrate_to_v5(conn)

    current = _read_schema_version(conn)
    if current == _CURRENT_SCHEMA_VERSION:
        return
    if current in ("1", "2", "3", "4"):
        # Earlier versions: the executescript above CREATE-IF-NOT-EXISTS'd
        # new tables (v2 metrics) and the migration added v4 columns. The
        # INSERT OR IGNORE in the script left the row at its old value, so
        # we UPDATE in place.
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (_CURRENT_SCHEMA_VERSION,),
        )
        return
    if current is None:
        # schema_meta exists but no row — defensive, the script's INSERT OR
        # IGNORE should have populated it. Insert now.
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
            "('schema_version', ?)",
            (_CURRENT_SCHEMA_VERSION,),
        )


def vacuum(conn: sqlite3.Connection) -> None:
    """VACUUM + ANALYZE the DB.

    Cheap maintenance, occasionally useful after a bulk reindex.
    ``VACUUM`` cannot run inside a transaction; the autocommit
    ``isolation_level=None`` we set in :func:`connect` makes this safe.
    """
    conn.execute("VACUUM")
    conn.execute("ANALYZE")
