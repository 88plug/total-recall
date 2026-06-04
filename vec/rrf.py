"""Reciprocal Rank Fusion + hybrid (FTS5 + dense) search.

RRF (Cormack, Clarke, Buettcher 2009) combines multiple ranked lists by summing
`1 / (k + rank)` across rankings, with `k=60` as the standard default. It needs
no score calibration between rankers — only their *relative* order — which is
exactly what we want when blending BM25-ish FTS5 scores with cosine distance.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from index.paths import project_key

log = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    rankings: list[list[Any]],
    k: int = 60,
    key: Callable[[Any], Any] = lambda x: x,
) -> list[tuple[Any, float]]:
    """Combine multiple rankings into a single fused ranking.

    Args:
        rankings: A list of ranked lists. Each inner list is ordered best→worst.
            Items can be any hashable type (or compound types — pass a `key`
            that returns something hashable, e.g. `extraction_id`).
        k: Damping constant. 60 is the value from the original paper and is the
            sane default unless you're tuning.
        key: Function that maps an item to its identity for fusion. Two items
            from different rankings with the same `key(...)` are treated as the
            same result. The first occurrence (highest-ranked across all input
            lists) wins for the returned "item" payload.

    Returns:
        A list of `(item, score)` tuples sorted by descending score.

    Edge cases:
        * Empty `rankings` → empty list.
        * Empty inner rankings are skipped silently.
        * Negative `k` is clamped to 0 (raises ZeroDivisionError otherwise).
    """
    if not rankings:
        return []

    k = max(0, k)

    scores: dict[Any, float] = {}
    # Track the canonical item to return for each identity. We keep the item
    # that appeared *earliest* in the *highest-ranked* position across all
    # input lists — i.e. the best-ranked observation.
    best_item: dict[Any, Any] = {}
    best_rank: dict[Any, int] = {}

    for ranking in rankings:
        if not ranking:
            continue
        for rank, item in enumerate(ranking):
            ident = key(item)
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank + 1)
            prior = best_rank.get(ident)
            if prior is None or rank < prior:
                best_rank[ident] = rank
                best_item[ident] = item

    fused = [(best_item[ident], score) for ident, score in scores.items()]
    fused.sort(key=lambda pair: pair[1], reverse=True)
    return fused


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Any | None = None,  # vec.embed.Embedder | None
    limit: int = 10,
    cwd: str | None = None,
    kind: str | None = None,
) -> list[Any]:
    """FTS5 + dense vector search, fused via RRF on `extraction_id`.

    Behaviour:
      * If `embedder is None` OR `sqlite_vec` is not installed OR the vec
        tables don't exist yet → falls back to FTS5-only (still returns
        results, just without the dense leg).
      * Otherwise runs both retrievers, fuses, and returns the top `limit`
        items. The returned items are whatever WT-4's `search_extractions`
        returns (we re-use its row shape so callers get a single API).

    The fusion key is `extraction_id`: a vec hit and an FTS5 hit that point to
    the same row are deduped.

    Defensive imports: WT-4's `index.query` may not exist yet on this branch.
    If it's missing we raise a clear error instead of an ImportError surprise.
    """
    if not query or not query.strip():
        return []

    fts_hits = _fts_search(conn, query, limit=limit * 3, cwd=cwd, kind=kind)

    if embedder is None or not _vec_available(conn):
        # FTS5-only path. Still apply `limit`.
        return list(fts_hits[:limit])

    # Dense leg.
    try:
        from .store import vec_search
    except (ImportError, Exception) as exc:  # pragma: no cover - vec module always importable
        log.debug("vec.store import failed, falling back to FTS5-only: %r", exc)
        return list(fts_hits[:limit])

    try:
        vec_hits = vec_search(conn, query, embedder, limit=limit * 3, cwd=cwd)
    except (RuntimeError, sqlite3.OperationalError, ImportError) as exc:
        # Dim mismatch / sqlite-vec missing at runtime / extension not loaded on
        # this conn / vec0 module unavailable. Degrade to FTS5-only instead of
        # taking the caller down with us.
        log.debug("vec_search failed, falling back to FTS5-only: %r", exc)
        return list(fts_hits[:limit])

    # Filter dense hits by kind if requested (vec_search doesn't, to keep its
    # signature minimal).
    if kind is not None:
        vec_hits = [h for h in vec_hits if h.kind == kind]

    fused = reciprocal_rank_fusion(
        [fts_hits, vec_hits],
        k=60,
        key=_extraction_id,
    )
    return [item for item, _score in fused[:limit]]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _extraction_id(item: Any) -> int:
    """Identity function for RRF fusion. Works on either FTS5 hit shape or VecHit."""
    if hasattr(item, "extraction_id"):
        return int(item.extraction_id)
    if isinstance(item, dict) and "extraction_id" in item:
        return int(item["extraction_id"])
    if isinstance(item, dict) and "id" in item:
        return int(item["id"])
    # Tuple/row fallback: assume id is first column.
    try:
        return int(item[0])
    except (TypeError, IndexError, ValueError):
        # Last resort — fuse on the object identity. This won't dedupe across
        # rankings but won't crash either.
        return id(item)


def _vec_available(conn: sqlite3.Connection) -> bool:
    """Return True iff sqlite-vec is installed AND the `vec_chunks` table exists."""
    try:
        import sqlite_vec  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='vec_chunks'"
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    cwd: str | None,
    kind: str | None,
) -> list[Any]:
    """Call WT-4's `search_extractions` if present; otherwise fall back to a
    minimal inline FTS5 query so this branch is testable in isolation.
    """
    try:
        from index.query import search_extractions  # type: ignore[import-not-found]
    except Exception:
        return _inline_fts_search(conn, query, limit=limit, cwd=cwd, kind=kind)

    try:
        return list(
            search_extractions(conn, query, limit=limit, cwd=cwd, kind=kind)  # type: ignore[call-arg]
        )
    except TypeError:
        # WT-4's signature might evolve; try positional-only.
        try:
            return list(search_extractions(conn, query))  # type: ignore[call-arg]
        except sqlite3.OperationalError:
            return _inline_fts_search(conn, query, limit=limit, cwd=cwd, kind=kind)
    except sqlite3.OperationalError:
        # WT-4's expected schema isn't present on this connection (e.g. tests
        # using a minimal FTS5-only schema). Fall back to the inline query.
        return _inline_fts_search(conn, query, limit=limit, cwd=cwd, kind=kind)


def _inline_fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    cwd: str | None,
    kind: str | None,
) -> list[Any]:
    """Minimal FTS5 query used when `index.query` isn't on the branch yet."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='extractions_fts'"
    )
    if cur.fetchone() is None:
        return []

    sql = (
        "SELECT e.id AS extraction_id, e.content, e.cwd, e.ts, e.kind "
        "FROM extractions_fts f JOIN extractions e ON e.id = f.rowid "
        "WHERE extractions_fts MATCH ?"
    )
    params: list[object] = [query]
    if cwd is not None:
        sql += " AND e.project_key = ?"
        params.append(project_key(cwd))
    if kind is not None:
        sql += " AND e.kind = ?"
        params.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        cur2 = conn.execute(sql, params)
        cols = [d[0] for d in (cur2.description or [])]
        return [dict(zip(cols, row)) for row in cur2.fetchall()]
    except sqlite3.Error:
        return []
