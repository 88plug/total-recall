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
  -- format v2 (ollama-only): keys format, model, backend, dim.
  -- Old indexes without format=2 must rebuild.

All optional deps (`sqlite_vec`, the embedding model) are imported inside
functions. The module imports cleanly without them.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from index.paths import project_key

if TYPE_CHECKING:  # pragma: no cover
    from .embed import Embedder

log = logging.getLogger(__name__)

# Format v2: ollama-only dense index. Pre-v2 indexes must rebuild —
# mixed embedding spaces are never silently reused.
VEC_FORMAT = "2"
_REBUILD_HINT = (
    "Run `total-recall rebuild --yes` or `python -m vec.cli rebuild` then "
    "`python -m vec.cli backfill` to recreate the dense index (format v2, ollama)."
)

_SQLITE_VEC_HINT = (
    "sqlite-vec is not installed. Install total-recall with its core deps:\n"
    "    pip install 'total-recall'"
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


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into `conn`.

    Always calls ``sqlite_vec.load`` — do not cache by ``id(conn)``. After GC,
    CPython can reuse that integer for a new connection; a set-based cache then
    skips load and later fails with ``no such module: vec0`` (GitHub
    ubuntu-latest / Python 3.10). ``sqlite3.Connection`` is also not weakref-able.

    Raises `RuntimeError` with the install hint if the package is missing, and
    a `RuntimeError` if the sqlite build can't load extensions (e.g. some
    distro builds disable `enable_load_extension`).
    """
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
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort
            conn.enable_load_extension(False)


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


def apply_vec_schema(
    conn: sqlite3.Connection,
    *,
    dim: int = 384,
    model: str | None = None,
    backend: str | None = None,
) -> None:
    """Create the vec-side tables if they don't already exist (format v2).

    `dim` / `model` / `backend` are recorded in `vec_meta` so query-time
    mismatches force a rebuild rather than silent wrong-space search.
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
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'")
    exists = cur.fetchone() is not None

    if exists:
        _assert_format_v2_or_raise(conn)
        stored = _read_meta(conn, "dim")
        if stored is not None and int(stored) != int(dim):
            raise RuntimeError(
                f"vec_chunks was built with dim={stored}, but current embedder "
                f"reports dim={dim}. {_REBUILD_HINT}"
            )
        if model is not None:
            stored_model = _read_meta(conn, "model")
            if stored_model is not None and stored_model != model:
                raise RuntimeError(
                    f"vec index model={stored_model!r} but current embedder is "
                    f"{model!r}. {_REBUILD_HINT}"
                )
    else:
        cur.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{int(dim)}])")
        _write_meta(conn, "dim", str(int(dim)))
        _write_meta(conn, "format", VEC_FORMAT)
        if model is not None:
            _write_meta(conn, "model", model)
        if backend is not None:
            _write_meta(conn, "backend", backend)

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


def _assert_format_v2_or_raise(conn: sqlite3.Connection) -> None:
    """Old (pre-ollama-only) indexes must rebuild — no silent reuse."""
    # Empty index (no chunks yet) can be upgraded in place.
    try:
        n = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()
        chunk_count = int(n[0]) if n else 0
    except sqlite3.Error:
        chunk_count = 0

    fmt = _read_meta(conn, "format")
    if fmt == VEC_FORMAT:
        return
    if chunk_count == 0 and _read_meta(conn, "dim") is None:
        # Brand-new schema shell — stamp format on first write path.
        return
    if chunk_count == 0:
        # Empty but has dim from incomplete prior run — allow re-stamp via ensure.
        return
    raise RuntimeError(
        f"Dense vector index is format {fmt!r} (need format {VEC_FORMAT!r} — "
        f"ollama-only embeddings). Old indexes are not reused. "
        f"{_REBUILD_HINT}"
    )


def _ensure_dim_matches(conn: sqlite3.Connection, embedder: Embedder) -> int:
    """Confirm embedder identity (format + model + dim) matches the on-disk index."""
    _assert_format_v2_or_raise(conn)
    dim = embedder.dim()
    identity = embedder.identity()  # backend:model
    backend, _, model = identity.partition(":")

    stored_dim = _read_meta(conn, "dim")
    stored_model = _read_meta(conn, "model")
    stored_backend = _read_meta(conn, "backend")
    stored_fmt = _read_meta(conn, "format")

    if stored_dim is None and stored_model is None:
        # First write: stamp full v2 identity.
        _write_meta(conn, "dim", str(dim))
        _write_meta(conn, "model", model or embedder.model)
        _write_meta(conn, "backend", backend or (embedder.backend or "unknown"))
        _write_meta(conn, "format", VEC_FORMAT)
        return dim

    if stored_fmt is not None and stored_fmt != VEC_FORMAT:
        raise RuntimeError(
            f"vec format={stored_fmt!r} is obsolete. {_REBUILD_HINT}"
        )
    if int(stored_dim or dim) != int(dim):
        raise RuntimeError(
            f"Embedding dim mismatch: index dim={stored_dim}, current "
            f"{identity!r} reports dim={dim}. {_REBUILD_HINT}"
        )
    want_model = model or embedder.model
    if stored_model is not None and stored_model != want_model:
        raise RuntimeError(
            f"Embedding model mismatch: index model={stored_model!r}, current "
            f"{want_model!r}. {_REBUILD_HINT}"
        )
    if stored_backend is not None and backend and stored_backend != backend:
        raise RuntimeError(
            f"Embedding backend mismatch: index backend={stored_backend!r}, current "
            f"{backend!r}. {_REBUILD_HINT}"
        )
    # Backfill missing v2 keys on partially-stamped indexes (empty upgrade).
    if stored_fmt is None:
        _write_meta(conn, "format", VEC_FORMAT)
    if stored_model is None and want_model:
        _write_meta(conn, "model", want_model)
    if stored_backend is None and backend:
        _write_meta(conn, "backend", backend)
    return dim


# ----------------------------------------------------------------------------
# Upsert
# ----------------------------------------------------------------------------


def _serialize_vector(vec: list[float]) -> bytes:
    """Pack a float vector into the little-endian float32 blob sqlite-vec expects.

    Prefer ``sqlite_vec.serialize_float32`` (matches extension layout); fall
    back to struct when the helper is absent.
    """
    try:
        import sqlite_vec

        ser = getattr(sqlite_vec, "serialize_float32", None)
        if callable(ser):
            return ser(vec)
    except Exception:  # noqa: BLE001
        pass
    import struct

    return struct.pack(f"<{len(vec)}f", *vec)


def _embed_texts_chunked(
    embedder: Embedder,
    texts: list[str],
    *,
    max_per_call: int,
    concurrency: int,
) -> list[list[float]]:
    """Embed ``texts`` in ordered sub-batches, optionally concurrent HTTP.

    Ollama ``/api/embed`` accepts a list; larger batches raise throughput.
    Concurrent sub-batches fill ``OLLAMA_NUM_PARALLEL`` slots on the GPU.
    Results are concatenated in original order.
    """
    if not texts:
        return []
    max_per_call = max(1, int(max_per_call))
    concurrency = max(1, int(concurrency))
    chunks: list[list[str]] = [
        texts[i : i + max_per_call] for i in range(0, len(texts), max_per_call)
    ]
    if concurrency == 1 or len(chunks) == 1:
        out: list[list[float]] = []
        for ch in chunks:
            out.extend(embedder.embed(ch))
        return out

    import concurrent.futures as cf

    results: list[list[list[float]] | None] = [None] * len(chunks)
    with cf.ThreadPoolExecutor(max_workers=min(concurrency, len(chunks))) as pool:
        futs = {pool.submit(embedder.embed, ch): i for i, ch in enumerate(chunks)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()
    out = []
    for part in results:
        assert part is not None
        out.extend(part)
    return out


def upsert_extraction_embedding(
    conn: sqlite3.Connection,
    extraction_id: int,
    content: str,
    embedder: Embedder,
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
        raise RuntimeError(f"Embedder returned {len(vectors)} vectors for {len(chunks)} chunks")

    _delete_existing(conn, extraction_id)
    cur = conn.cursor()
    for idx, (chunk, vec) in enumerate(zip(chunks, vectors, strict=False)):
        cur.execute(
            "INSERT INTO chunk_embeddings(extraction_id, chunk_text, chunk_index) VALUES (?, ?, ?)",
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
    cur = conn.execute("SELECT id FROM chunk_embeddings WHERE extraction_id = ?", (extraction_id,))
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
    embedder: Embedder | None = None,
    batch_size: int | None = None,
    only_kinds: list[str] | None = None,
) -> BackfillReport:
    """Embed every extraction in WT-4's `extractions` table that lacks chunks.

    Args:
        conn: Open sqlite3 connection to the shared `index.db`.
        embedder: Embedder to use. If None, a default `Embedder()` is built —
            (ollama by default; see vec.embed).
        batch_size: How many extractions to fetch per outer round. Default 256
            (or ``TOTAL_RECALL_EMBED_BATCH``). Texts inside the batch are
            embedded in concurrent sub-calls of ``TOTAL_RECALL_EMBED_MAX_INPUT``
            (default 128) with ``TOTAL_RECALL_EMBED_CONCURRENCY`` (default 4)
            parallel HTTP requests so product ollama's ``OLLAMA_NUM_PARALLEL``
            slots stay busy.
        only_kinds: If set, restrict to these `extractions.kind` values
            (e.g. ['correction', 'decision']).
    """
    import os

    from .embed import Embedder, chunk_for_embedding

    if embedder is None:
        embedder = Embedder()

    if batch_size is None:
        raw = (os.environ.get("TOTAL_RECALL_EMBED_BATCH") or "256").strip()
        batch_size = int(raw) if raw.isdigit() and int(raw) > 0 else 256
    batch_size = max(1, int(batch_size))

    max_input_raw = (os.environ.get("TOTAL_RECALL_EMBED_MAX_INPUT") or "128").strip()
    max_per_call = (
        int(max_input_raw) if max_input_raw.isdigit() and int(max_input_raw) > 0 else 128
    )
    conc_raw = (os.environ.get("TOTAL_RECALL_EMBED_CONCURRENCY") or "4").strip()
    concurrency = int(conc_raw) if conc_raw.isdigit() and int(conc_raw) > 0 else 4

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
    total = len(rows)
    log.info(
        "vec backfill: pending extractions=%d batch=%d max_input=%d concurrency=%d",
        total,
        batch_size,
        max_per_call,
        concurrency,
    )

    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        # Build a flat list of (extraction_id, chunk_text) so we embed in
        # batched concurrent HTTP calls to product ollama.
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
            vectors = _embed_texts_chunked(
                embedder,
                [t for _, t in flat],
                max_per_call=max_per_call,
                concurrency=concurrency,
            )
        except Exception as exc:  # pragma: no cover - defensive
            msg = f"embed batch starting at row {i} failed: {exc!r}"
            log.warning(msg)
            report.errors.append(msg)
            continue

        if len(vectors) != len(flat):
            msg = (
                f"embed batch at row {i}: got {len(vectors)} vectors "
                f"for {len(flat)} texts"
            )
            log.warning(msg)
            report.errors.append(msg)
            continue

        # Group vectors back by extraction_id so chunk_index restarts per ext.
        per_ext: dict[int, list[tuple[str, list[float]]]] = {}
        for (ext_id, chunk), vec in zip(flat, vectors, strict=True):
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
        log.info(
            "vec backfill progress: %d/%d extractions seen, "
            "embedded=%d chunks=%d",
            min(i + batch_size, total),
            total,
            report.extractions_embedded,
            report.chunks_written,
        )

    return report


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------


def vec_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Embedder,
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

    # Query-side instruction prefixes (bge / nomic / qwen3-embedding) when supported.
    vecs = embedder.embed([query], as_query=True)
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

    # Kind + distinctive lexical re-rank (matches hybrid / FTS philosophy):
    # decisions/corrections/bans outrank domain_fact near-misses when cosine is
    # close; sibling decisions that miss heavy query tokens fall behind.
    # Lower score is better.
    out = sorted(seen.values(), key=lambda h: _dense_rank_key(h, query))
    return out[:limit]


# Align with index.query._KIND_PRIORITY — high-value memory kinds win ties.
_DENSE_KIND_BOOST: dict[str, float] = {
    "correction": 0.14,
    "ban": 0.14,
    "decision": 0.12,
    "goal": 0.10,
    "self_correction": 0.08,
    "truth_assertion": 0.08,
    "progress": 0.06,
    "domain_fact": 0.0,
    "model_correction": 0.04,
    "away_summary": 0.0,
}


def _dense_rank_key(hit: VecHit, query: str = "") -> float:
    """Sort key: cosine distance minus kind/lexical boosts (lower is better)."""
    boost = _DENSE_KIND_BOOST.get(hit.kind, 0.02)
    if not query:
        return hit.cosine_distance - boost
    try:
        from .rrf import (
            _distinctive_token_coverage,
            _legacy_negation_penalty,
            _token_coverage,
        )
    except Exception:  # noqa: BLE001
        return hit.cosine_distance - boost
    dcov = _distinctive_token_coverage(query, hit.content)
    cov = _token_coverage(query, hit.content)
    neg = _legacy_negation_penalty(hit.content)
    # Small pulls only — keep cosine dominant for pure paraphrases.
    return hit.cosine_distance - boost - 0.04 * dcov - 0.02 * cov + 0.08 * neg
