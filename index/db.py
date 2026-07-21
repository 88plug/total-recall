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
    "resolve_data_dir",
    "resolve_db_path",
    "list_index_candidates",
    "connect",
    "apply_schema",
    "vacuum",
    "apply_bulk_load_pragmas",
    "restore_default_pragmas",
    "drop_fts_sync_triggers",
    "recreate_fts_sync_triggers",
    "rebuild_fts_indexes",
]


def _xdg_data_dir() -> Path:
    return Path("~/.local/share/total-recall").expanduser()


def _plugin_data_candidates() -> list[Path]:
    """Existing plugin-data index dirs under ~/.claude/plugins/data/.

    Marketplace installs land at
    ``~/.claude/plugins/data/<plugin-id>/total-recall/index.db`` when the
    harness sets ``CLAUDE_PLUGIN_DATA`` to ``…/<plugin-id>``. Without env,
    we still *discover* those so CLI/hooks don't silently open a second XDG DB.
    """
    root = Path("~/.claude/plugins/data").expanduser()
    if not root.is_dir():
        return []
    found: list[Path] = []
    # …/data/total-recall-88plug/total-recall/index.db  (marketplace id)
    # …/data/total-recall/index.db                       (legacy bare fallback)
    for child in sorted(root.iterdir()) if root.exists() else []:
        if not child.is_dir():
            continue
        nested = child / "total-recall" / "index.db"
        if nested.is_file():
            found.append(nested.parent)
        # bare: …/data/total-recall/index.db (hooks old fallback)
        if child.name == "total-recall" and (child / "index.db").is_file():
            found.append(child)
    # de-dupe preserve order
    out: list[Path] = []
    seen: set[Path] = set()
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def list_index_candidates() -> list[Path]:
    """Every ``index.db`` we know about (plugin data + XDG), existing only."""
    paths: list[Path] = []
    for d in _plugin_data_candidates():
        p = d / "index.db"
        if p.is_file():
            paths.append(p)
    xdg = _xdg_data_dir() / "index.db"
    if xdg.is_file():
        paths.append(xdg)
    # unique by resolve
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        try:
            r = p.resolve()
        except OSError:
            r = p
        if r not in seen:
            seen.add(r)
            out.append(p)
    return out


def resolve_data_dir() -> Path:
    """Single canonical data directory for the index (and sibling state).

    Precedence (highest first):

    1. ``$TOTAL_RECALL_DB_DIR`` — explicit dir override (``.mcp.json`` sets this).
    2. ``$TOTAL_RECALL_DB`` parent — file override from CLI/env.
    3. ``$CLAUDE_PLUGIN_DATA/total-recall`` — harness-set plugin data (users).
    4. Largest existing plugin-data index under ``~/.claude/plugins/data/`` —
       so bare ``total-recall`` CLI matches the plugin instead of inventing XDG.
    5. ``~/.local/share/total-recall`` — XDG fallback (fresh dev / no plugin).

    Plugin-only installs always hit (3) via the harness — one DB, no dual path.
    Dual DBs only appear if something ran without env *and* without discovery
    (legacy); (4) stops that going forward.
    """
    explicit_dir = (os.environ.get("TOTAL_RECALL_DB_DIR") or "").strip()
    if explicit_dir:
        return Path(explicit_dir).expanduser().resolve()

    explicit_file = (os.environ.get("TOTAL_RECALL_DB") or "").strip()
    if explicit_file:
        return Path(explicit_file).expanduser().resolve().parent

    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        return (Path(plugin_data).expanduser() / "total-recall").resolve()

    # No harness env: prefer an already-populated plugin index over empty XDG.
    plugin_dirs = _plugin_data_candidates()
    if plugin_dirs:
        def _sz(d: Path) -> int:
            try:
                return (d / "index.db").stat().st_size
            except OSError:
                return 0

        best = max(plugin_dirs, key=_sz)
        if _sz(best) > 0:
            return best.resolve()

    return _xdg_data_dir().resolve()


def resolve_db_path() -> Path:
    """Canonical ``index.db`` path (see :func:`resolve_data_dir`)."""
    return resolve_data_dir() / "index.db"


def _default_db_path() -> Path:
    """Import-time default; prefer :func:`resolve_db_path` at call sites."""
    return resolve_db_path()


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
    db_path: Path | str | None = None,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open a connection to the total-recall index.

    * Creates parent dir (0700) if missing.
    * Sets WAL, busy_timeout=5000, foreign_keys=ON on every connection.
    * Applies the schema on first open (idempotent).
    * ``read_only=True`` opens the URI in ``mode=ro`` — schema apply is
      skipped and writes will fail (use for query-only consumers like the MCP
      server).
    * ``db_path is None`` → :func:`resolve_db_path` (re-evaluated each call so
      env / discovery stay correct).
    """
    if db_path is None:
        db_path = resolve_db_path()
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


# ---------------------------------------------------------------------------
# Bulk-load (rebuild / oneshot) helpers
# ---------------------------------------------------------------------------
# Rebuild is single-writer + restartable. These knobs trade crash durability
# and FTS live-sync for write throughput. Always pair apply with restore +
# rebuild_fts_indexes before the connection is handed back to readers.

_FTS_TRIGGER_SQL: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text)
            VALUES('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text)
            VALUES('delete', old.id, old.text);
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS extractions_ai AFTER INSERT ON extractions BEGIN
        INSERT INTO extractions_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS extractions_ad AFTER DELETE ON extractions BEGIN
        INSERT INTO extractions_fts(extractions_fts, rowid, content)
            VALUES('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS extractions_au AFTER UPDATE ON extractions BEGIN
        INSERT INTO extractions_fts(extractions_fts, rowid, content)
            VALUES('delete', old.id, old.content);
        INSERT INTO extractions_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
)

_FTS_TRIGGER_NAMES: tuple[str, ...] = (
    "messages_ai",
    "messages_ad",
    "messages_au",
    "extractions_ai",
    "extractions_ad",
    "extractions_au",
)


def apply_bulk_load_pragmas(conn: sqlite3.Connection) -> None:
    """Raise write throughput for rebuild without weakening durability.

    Source: sqlite.org ``PRAGMA synchronous`` — ``OFF`` can corrupt on OS
    crash/power loss even after commit; product quality forbids that.
    We keep ``synchronous=NORMAL`` (WAL-safe, same as :func:`connect`).

    Larger ``cache_size`` / ``mmap_size`` / ``temp_store=MEMORY`` only change
    page residency — not correctness. Default ``cache_size`` is ~2 MiB
    (``-2000``), which starves multi-hundred-MB rebuilds.
    """
    # Do NOT set synchronous=OFF. Quality > speed.
    conn.execute("PRAGMA synchronous = NORMAL")
    # Negative cache_size is kibibytes of page cache (~256 MiB).
    conn.execute("PRAGMA cache_size = -262144")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint = 10000")


def restore_default_pragmas(conn: sqlite3.Connection) -> None:
    """Restore post-bulk page-cache defaults used by :func:`connect`."""
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -2000")
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("PRAGMA temp_store = DEFAULT")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")


def drop_fts_sync_triggers(conn: sqlite3.Connection) -> None:
    """Drop FTS5 content-sync triggers for bulk insert (rebuild later)."""
    for name in _FTS_TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def recreate_fts_sync_triggers(conn: sqlite3.Connection) -> None:
    """Re-install FTS5 content-sync triggers after bulk load."""
    for sql in _FTS_TRIGGER_SQL:
        conn.execute(sql)


def _fts_match_probe(
    conn: sqlite3.Connection,
    *,
    fts: str,
    base: str,
    text_col: str,
    sample: int = 32,
) -> int:
    """Return how many sample content rows are MATCH-findable in ``fts``.

    External-content FTS5: ``SELECT COUNT(*) FROM fts`` is *not* a reliable
    index-size signal (it can track the content table). Real quality is
    whether token MATCH finds the row after rebuild.
    """
    import re

    rows = conn.execute(
        f"SELECT id, {text_col} FROM {base} "
        f"WHERE {text_col} IS NOT NULL AND length(trim({text_col})) > 8 "
        f"ORDER BY id DESC LIMIT ?",
        (sample,),
    ).fetchall()
    hits = 0
    for rowid, text in rows:
        tokens = re.findall(r"[A-Za-z]{4,}", text or "")
        # Prefer longer tokens (less stemming surprise with porter).
        tokens = sorted(set(tokens), key=len, reverse=True)
        found = False
        for tok in tokens[:6]:
            try:
                n = conn.execute(
                    f"SELECT 1 FROM {fts} WHERE {fts} MATCH ? AND rowid = ? LIMIT 1",
                    (tok, rowid),
                ).fetchone()
            except sqlite3.DatabaseError:
                continue
            if n:
                found = True
                break
        if found:
            hits += 1
    return hits


def rebuild_fts_indexes(
    conn: sqlite3.Connection,
    *,
    verify: bool = True,
) -> dict[str, dict[str, int]]:
    """Rebuild external-content FTS5 tables from base tables.

    FTS5 special INSERTs (sqlite.org/fts5 §6 Special INSERT Commands):

    * ``INSERT INTO fts(fts) VALUES('rebuild')`` — reconstruct index from
      the external content table (required after deferred triggers).
    * ``INSERT INTO fts(fts) VALUES('integrity-check')`` — raise if the
      FTS index disagrees with content for rows it *does* hold.

    When ``verify=True`` (default), also run MATCH probes on sample content
    rows. ``COUNT(*)`` on external-content FTS is **not** used (unreliable
    vs the content table; empty index can still report content-sized counts).
    Zero hits on non-empty sample content raises — never ship unsearchable FTS.

    Returns per-table stats for logging/tests.
    """
    pairs = (
        ("messages_fts", "messages", "text"),
        ("extractions_fts", "extractions", "content"),
    )
    out: dict[str, dict[str, int]] = {}
    for fts, base, text_col in pairs:
        try:
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
        except sqlite3.DatabaseError:
            # Table may not exist on a minimal/test schema.
            continue
        if not verify:
            out[fts] = {"verified": 0}
            continue
        try:
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"FTS integrity-check failed for {fts} after rebuild: {exc}"
            ) from exc
        try:
            n_base = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {base} "
                    f"WHERE {text_col} IS NOT NULL AND length(trim({text_col})) > 8"
                ).fetchone()[0]
            )
        except sqlite3.DatabaseError:
            n_base = 0
        hits = _fts_match_probe(conn, fts=fts, base=base, text_col=text_col)
        if n_base > 0 and hits == 0:
            raise RuntimeError(
                f"FTS MATCH probe failed for {fts}: 0/{min(n_base, 32)} sample "
                f"content rows searchable after rebuild (search quality compromised)"
            )
        out[fts] = {"content_with_text": n_base, "match_probe_hits": hits}
    return out


_CURRENT_SCHEMA_VERSION = "5"


def _read_schema_version(conn: sqlite3.Connection) -> str | None:
    """Return the recorded ``schema_meta.schema_version``, or ``None``.

    Returns ``None`` if either the ``schema_meta`` table is absent (fresh DB
    pre-bootstrap) or the row simply hasn't been inserted yet.
    """
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
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
                "ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'claude_code'"
            )
        if "dedup_superseded_by_source" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN dedup_superseded_by_source TEXT")
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
                "ALTER TABLE extractions ADD COLUMN source TEXT NOT NULL DEFAULT 'claude_code'"
            )
        if "dedup_superseded_by_source" not in ext_cols:
            conn.execute("ALTER TABLE extractions ADD COLUMN dedup_superseded_by_source TEXT")
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
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
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
