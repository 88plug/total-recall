"""SQLite-vec storage that lives alongside WT-4's FTS5 `extractions` index.

Schema (added by `apply_vec_schema`):

  CREATE TABLE chunk_embeddings (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      extraction_id INTEGER NOT NULL,             -- FK -> extractions.id (WT-4)
      chunk_text    TEXT NOT NULL,
      chunk_index   INTEGER NOT NULL,
      UNIQUE (extraction_id, chunk_index)
  );
  CREATE INDEX idx_chunk_embeddings_extraction ON chunk_embeddings(extraction_id);

  CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[<DIM>]);
  -- rowid in vec_chunks matches chunk_embeddings.id

  CREATE TABLE vec_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
  );
  -- vec_meta stores ('model', <model id>) and ('dim', <int>) so we can detect
  -- dimension mismatches at search time and tell the user to run `vec rebuild`.

All optional deps (`sqlite_vec`, the embedding model) are imported inside
functions. The module imports cleanly without them.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from index.paths import project_key

if TYPE_CHECKING:  # pragma: no cover
    from .embed import Embedder

log = logging.getLogger(__name__)

_SQLITE_VEC_HINT = (
    "sqlite-vec is not installed. Install the optional 'vec' extra:\n"
    "    pip install 'total-recall[vec]'"
)


# ----------------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------------


@dataclass
class VecHit:
    """One hit from `vec_search` — a chunk with its parent extraction row."""

    extraction_id: int
    chunk_text: str
    cosine_distance: float
    content: str
    cwd: str
    ts: datetime
    kind: str


@dataclass
class BackfillReport:
    """Summary of a `backfill_all` run."""

    extractions_seen: int = 0
    extractions_embedded: int = 0
    extractions_skipped: int = 0
    chunks_written: int = 0
    errors: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------
# sqlite-vec loader / helpers
# ----------------------------------------------------------------------------


# Track connections that already have sqlite-vec loaded, so `_load_sqlite_vec`
# is safe (and cheap) to call at the top of every public entry point. Keyed by
# `id(conn)` — entries are dropped automatically when the underlying connection
# object is GC'd (we use a WeakSet of wrappers? no — id() is stable for the
# lifetime of the conn and we accept some staleness if a conn is closed and a
# new one happens to get the same id; load is idempotent anyway).
_loaded: set[int] = set()


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into `conn` (idempotent).

    Safe to call repeatedly: if `conn` has already been loaded, this is a
    no-op. This lets every public entry point call it without worrying about
    double-loading.

    Raises `RuntimeError` with the install hint if the package is missing, and
    a `RuntimeError` if the sqlite build can't load extensions (e.g. some
    distro builds disable `enable_load_extension`).
    """
    if id(conn) in _loaded:
        return

    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise RuntimeError(_SQLITE_VEC_HINT) from exc

    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError) as exc:
        raise RuntimeError(
            "This Python build's sqlite3 does not support load_extension(). "
            "Use a Python built against a full SQLite (pyenv / official builds)."
        ) from exc
    try:
        sqlite_vec.load(conn)
    finally:
        # Re-disable; the extension stays loaded but we don't want stray loads.
        try:
            conn.enable_load_extension(False)
        except Exception:  # pragma: no cover - best-effort
            pass

    _loaded.add(id(conn))


def _parse_ts(raw: object) -> datetime:
    """Convert a stored timestamp (ISO string or epoch) to a datetime."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str):
        # Try ISO 8601 first; fall back to epoch-as-string.
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except ValueError:
                pass
    return datetime.now(tz=timezone.utc)


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------


def apply_vec_schema(conn: sqlite3.Connection, *, dim: int = 384) -> None:
    """Create the vec-side tables if they don't already exist.

    `dim` is the embedding dim of the *current* model — recorded in `vec_meta`
    so query-time mismatches can be detected and reported clearly.
    """
    _load_sqlite_vec(conn)

    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunk_embeddings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id INTEGER NOT NULL,
            chunk_text    TEXT NOT NULL,
            chunk_index   INTEGER NOT NULL,
            UNIQUE (extraction_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_extraction
            ON chunk_embeddings(extraction_id);

        CREATE TABLE IF NOT EXISTS vec_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    # The vec0 virtual table dim is baked into the schema string and can't be
    # ALTERed in place. Only create it if absent; if a row in `vec_meta` says
    # a different dim is in force, raise.
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
    )
    exists = cur.fetchone() is not None

    if exists:
        stored = _read_meta(conn, "dim")
        if stored is not None and int(stored) != int(dim):
            raise RuntimeError(
                f"vec_chunks was built with dim={stored}, but current embedder "
                f"reports dim={dim}. Run `python -m vec.cli rebuild` "
                f"(or drop vec_chunks + chunk_embeddings) to recreate the index."
            )
    else:
        cur.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{int(dim)}])")
        _write_meta(conn, "dim", str(int(dim)))

    conn.commit()


def _read_meta(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM vec_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return None if row is None else row[0]


def _write_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO vec_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _ensure_dim_matches(conn: sqlite3.Connection, embedder: "Embedder") -> int:
    """Confirm the embedder's dim matches the on-disk index. Returns the dim."""
    stored = _read_meta(conn, "dim")
    dim = embedder.dim()
    if stored is None:
        # First write: record it.
        _write_meta(conn, "dim", str(dim))
        return dim
    if int(stored) != int(dim):
        raise RuntimeError(
            f"Embedding dim mismatch: index built for dim={stored}, current model "
            f"({embedder.model!r}) reports dim={dim}. Run `python -m vec.cli rebuild`."
        )
    return dim


# ----------------------------------------------------------------------------
# Upsert
# ----------------------------------------------------------------------------


def _serialize_vector(vec: list[float]) -> bytes:
    """Pack a float vector into the little-endian float32 blob sqlite-vec expects."""
    import struct

    return struct.pack(f"<{len(vec)}f", *vec)


def upsert_extraction_embedding(
    conn: sqlite3.Connection,
    extraction_id: int,
    content: str,
    embedder: "Embedder",
) -> int:
    """Embed `content` (chunked) and (re)write its rows in chunk_embeddings + vec_chunks.

    Returns the number of chunks written. Idempotent: existing rows for
    `extraction_id` are deleted before insert.
    """
    from .embed import chunk_for_embedding

    _load_sqlite_vec(conn)
    _ensure_dim_matches(conn, embedder)

    chunks = chunk_for_embedding(content)
    if not chunks:
        # Nothing to embed; still clear any stale rows so caller can rely on
        # "after upsert, the state matches the input".
        _delete_existing(conn, extraction_id)
        conn.commit()
        return 0

    vectors = embedder.embed(chunks)
    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"Embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
        )

    _delete_existing(conn, extraction_id)
    cur = conn.cursor()
    for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
        cur.execute(
            "INSERT INTO chunk_embeddings(extraction_id, chunk_text, chunk_index) "
            "VALUES (?, ?, ?)",
            (extraction_id, chunk, idx),
        )
        rowid = cur.lastrowid
        cur.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
            (rowid, _serialize_vector(vec)),
        )

    conn.commit()
    return len(chunks)


def _delete_existing(conn: sqlite3.Connection, extraction_id: int) -> None:
    """Remove any prior chunks/vectors for this extraction (idempotent upsert)."""
    cur = conn.execute(
        "SELECT id FROM chunk_embeddings WHERE extraction_id = ?", (extraction_id,)
    )
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return
    qmarks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({qmarks})", ids)
    conn.execute("DELETE FROM chunk_embeddings WHERE extraction_id = ?", (extraction_id,))


# ----------------------------------------------------------------------------
# Backfill
# ----------------------------------------------------------------------------


def backfill_all(
    conn: sqlite3.Connection,
    embedder: "Embedder | None" = None,
    batch_size: int = 64,
    only_kinds: list[str] | None = None,
) -> BackfillReport:
    """Embed every extraction in WT-4's `extractions` table that lacks chunks.

    Args:
        conn: Open sqlite3 connection to the shared `index.db`.
        embedder: Embedder to use. If None, a default `Embedder()` is built —
            which lazily loads fastembed on first batch.
        batch_size: How many extractions to fetch + embed per round-trip.
        only_kinds: If set, restrict to these `extractions.kind` values
            (e.g. ['correction', 'decision']).
    """
    from .embed import Embedder, chunk_for_embedding

    if embedder is None:
        embedder = Embedder()

    _load_sqlite_vec(conn)
    _ensure_dim_matches(conn, embedder)

    report = BackfillReport()

    where = "WHERE e.id NOT IN (SELECT extraction_id FROM chunk_embeddings)"
    params: list[object] = []
    if only_kinds:
        qmarks = ",".join("?" * len(only_kinds))
        where += f" AND e.kind IN ({qmarks})"
        params.extend(only_kinds)

    cur = conn.execute(
        f"SELECT e.id, e.content FROM extractions e {where} ORDER BY e.id",
        params,
    )
    rows = cur.fetchall()

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        # Build a flat list of (extraction_id, chunk_text) so we embed in one
        # call — fastembed is much happier with bigger batches.
        flat: list[tuple[int, str]] = []
        skip_ids: set[int] = set()
        for ext_id, content in batch:
            report.extractions_seen += 1
            chunks = chunk_for_embedding(content or "")
            if not chunks:
                report.extractions_skipped += 1
                skip_ids.add(ext_id)
                continue
            for ch in chunks:
                flat.append((ext_id, ch))

        if not flat:
            continue

        try:
            vectors = embedder.embed([t for _, t in flat])
        except Exception as exc:  # pragma: no cover - defensive
            msg = f"embed batch starting at row {i} failed: {exc!r}"
            log.warning(msg)
            report.errors.append(msg)
            continue

        # Group vectors back by extraction_id so chunk_index restarts per ext.
        per_ext: dict[int, list[tuple[str, list[float]]]] = {}
        for (ext_id, chunk), vec in zip(flat, vectors):
            per_ext.setdefault(ext_id, []).append((chunk, vec))

        cur2 = conn.cursor()
        for ext_id, items in per_ext.items():
            _delete_existing(conn, ext_id)
            for idx, (chunk, vec) in enumerate(items):
                cur2.execute(
                    "INSERT INTO chunk_embeddings(extraction_id, chunk_text, chunk_index) "
                    "VALUES (?, ?, ?)",
                    (ext_id, chunk, idx),
                )
                rowid = cur2.lastrowid
                cur2.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                    (rowid, _serialize_vector(vec)),
                )
                report.chunks_written += 1
            report.extractions_embedded += 1
        conn.commit()

    return report


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------


def vec_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: "Embedder",
    limit: int = 20,
    cwd: str | None = None,
) -> list[VecHit]:
    """Run a dense top-k search over `vec_chunks` and return joined hits.

    The join targets `extractions.{content,cwd,ts,kind}` so callers don't need
    a second round-trip. We over-fetch by 3x to give RRF more room when chunks
    from the same extraction collide, then dedupe by extraction_id keeping the
    best (lowest) cosine_distance.
    """
    if not query or not query.strip():
        return []

    _load_sqlite_vec(conn)
    _ensure_dim_matches(conn, embedder)

    vecs = embedder.embed([query])
    if not vecs:
        return []
    qvec = _serialize_vector(vecs[0])

    over = max(limit * 3, limit)

    # KNN over the virtual table. We pull the chunk rowid + cosine_distance
    # then join out to chunk_embeddings + extractions.
    sql = """
        SELECT
            ce.extraction_id,
            ce.chunk_text,
            v.distance AS cosine_distance,
            e.content,
            e.cwd,
            e.ts,
            e.kind
        FROM vec_chunks v
        JOIN chunk_embeddings ce ON ce.id = v.rowid
        JOIN extractions e       ON e.id  = ce.extraction_id
        WHERE v.embedding MATCH ?
          AND k = ?
    """
    params: list[object] = [qvec, over]
    if cwd is not None:
        # Pool worktree checkouts under their owning repo root (see
        # index.paths.project_key) so dense recall matches the FTS leg.
        sql += " AND e.project_key = ?"
        params.append(project_key(cwd))
    sql += " ORDER BY v.distance"

    cur = conn.execute(sql, params)
    rows = cur.fetchall()

    seen: dict[int, VecHit] = {}
    for ext_id, chunk_text, dist, content, row_cwd, ts, kind in rows:
        hit = VecHit(
            extraction_id=int(ext_id),
            chunk_text=str(chunk_text),
            cosine_distance=float(dist),
            content=str(content) if content is not None else "",
            cwd=str(row_cwd) if row_cwd is not None else "",
            ts=_parse_ts(ts),
            kind=str(kind) if kind is not None else "",
        )
        prev = seen.get(hit.extraction_id)
        if prev is None or hit.cosine_distance < prev.cosine_distance:
            seen[hit.extraction_id] = hit

    out = sorted(seen.values(), key=lambda h: h.cosine_distance)
    return out[:limit]
