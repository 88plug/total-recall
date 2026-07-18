"""Reciprocal Rank Fusion + hybrid (FTS5 + dense) search.

RRF (Cormack, Clarke, Buettcher 2009) combines multiple ranked lists by summing
`1 / (k + rank)` across rankings, with `k=60` as the standard default. It needs
no score calibration between rankers — only their *relative* order — which is
exactly what we want when blending BM25-ish FTS5 scores with cosine distance.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from index.paths import project_key

log = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    rankings: list[list[Any]],
    k: int = 60,
    key: Callable[[Any], Any] = lambda x: x,
    weights: list[float] | None = None,
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
        weights: Optional per-ranking multipliers (same length as ``rankings``).
            Default equal weight 1.0. Dense-primary hybrid uses a higher dense
            weight so weak FTS matches cannot steal top-1.

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
    if weights is None:
        weights = [1.0] * len(rankings)
    elif len(weights) != len(rankings):
        raise ValueError("weights length must match rankings length")

    scores: dict[Any, float] = {}
    # Track the canonical item to return for each identity. Prefer the item
    # from the highest-weight ranking at its best rank (dense payload over FTS
    # dict when both match the same extraction_id).
    best_item: dict[Any, Any] = {}
    best_rank: dict[Any, int] = {}
    best_weight: dict[Any, float] = {}

    for ranking, weight in zip(rankings, weights, strict=True):
        if not ranking or weight <= 0:
            continue
        w = float(weight)
        for rank, item in enumerate(ranking):
            ident = key(item)
            scores[ident] = scores.get(ident, 0.0) + w / (k + rank + 1)
            prior = best_rank.get(ident)
            prior_w = best_weight.get(ident, 0.0)
            # Prefer lower rank; on rank ties prefer higher-weight ranking's item.
            if prior is None or rank < prior or (rank == prior and w > prior_w):
                best_rank[ident] = rank
                best_item[ident] = item
                best_weight[ident] = w

    fused = [(best_item[ident], score) for ident, score in scores.items()]
    fused.sort(key=lambda pair: pair[1], reverse=True)
    return fused


def _hybrid_mode() -> str:
    """dense_primary | weighted_rrf | rrf — see hybrid_search docstring."""
    import os

    raw = (os.environ.get("TOTAL_RECALL_HYBRID_MODE") or "dense_primary").strip().lower()
    if raw in ("dense_primary", "dense-first", "dense_first"):
        return "dense_primary"
    if raw in ("weighted_rrf", "weighted", "wrrf"):
        return "weighted_rrf"
    return "rrf"


def _hybrid_weights() -> tuple[float, float]:
    """(fts_weight, dense_weight) for weighted_rrf mode."""
    import os

    try:
        fts_w = float(os.environ.get("TOTAL_RECALL_HYBRID_FTS_WEIGHT") or "1.0")
    except ValueError:
        fts_w = 1.0
    try:
        dense_w = float(os.environ.get("TOTAL_RECALL_HYBRID_DENSE_WEIGHT") or "3.0")
    except ValueError:
        dense_w = 3.0
    return max(0.0, fts_w), max(0.0, dense_w)


def _dense_primary_merge(vec_hits: list[Any], fts_hits: list[Any], limit: int) -> list[Any]:
    """Dense rank order first; FTS only appends novel exact-keyword hits.

    Preserves pure-dense P@1/P@5 on paraphrase queries (where FTS is noisy)
    while still surfacing keyword-only rows dense missed.
    """
    out: list[Any] = []
    seen: set[int] = set()
    for item in vec_hits:
        ident = _extraction_id(item)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(item)
        if len(out) >= limit:
            return out
    for item in fts_hits:
        ident = _extraction_id(item)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    embedder: Any | None = None,  # vec.embed.Embedder | None
    limit: int = 10,
    cwd: str | None = None,
    kind: str | None = None,
) -> list[Any]:
    """FTS5 + dense vector search fused on ``extraction_id``.

    Behaviour:
      * If ``embedder is None`` OR vec unavailable → FTS5-only.
      * Default fusion mode ``dense_primary`` (env ``TOTAL_RECALL_HYBRID_MODE``):
        keep dense rank order, then append FTS hits dense missed. Stops weak
        FTS token matches from stealing top-1 from strong semantic hits.
      * ``weighted_rrf``: classic RRF with dense weight default 3× FTS.
      * ``rrf``: equal-weight RRF (legacy).

    The fusion key is `extraction_id`: a vec hit and an FTS5 hit that point to
    the same row are deduped.
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

    if not vec_hits:
        return list(fts_hits[:limit])
    if not fts_hits:
        return list(vec_hits[:limit])

    mode = _hybrid_mode()
    if mode == "dense_primary":
        return _dense_primary_merge(list(vec_hits), list(fts_hits), limit)

    if mode == "weighted_rrf":
        fts_w, dense_w = _hybrid_weights()
        fused = reciprocal_rank_fusion(
            [list(fts_hits), list(vec_hits)],
            k=60,
            key=_extraction_id,
            weights=[fts_w, dense_w],
        )
        return [item for item, _score in fused[:limit]]

    fused = reciprocal_rank_fusion(
        [list(fts_hits), list(vec_hits)],
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
        return [dict(zip(cols, row, strict=False)) for row in cur2.fetchall()]
    except sqlite3.Error:
        return []
