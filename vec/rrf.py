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


def _hit_content(item: Any) -> str:
    c = getattr(item, "content", None)
    if c is None and isinstance(item, dict):
        c = item.get("content")
    return str(c or "")


def _exactish_match(query: str, content: str) -> bool:
    """True if content carries the query as a phrase or all significant tokens.

    Used to detect FTS wins on env vars, model tags, hostnames, error names —
    cases where dense near-misses (web-02 vs web-01, embeddinggemma vs qwen tag)
    must not steal top-1 under dense_primary.
    """
    q = (query or "").strip().lower()
    c = (content or "").lower()
    if not q or not c:
        return False
    if q in c:
        return True
    toks = _query_tokens(query)
    if not toks:
        return False
    # Single identifier query: substring match is enough (don't require stopwords)
    if len(toks) == 1 and _token_weight(toks[0]) >= 1.5:
        return toks[0] in c
    return all(t in c for t in toks)


def _merge_primary(primary: list[Any], secondary: list[Any], limit: int) -> list[Any]:
    """Primary rank order first; secondary only appends novel extraction_ids."""
    out: list[Any] = []
    seen: set[int] = set()
    for item in primary:
        ident = _extraction_id(item)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(item)
        if len(out) >= limit:
            return out
    for item in secondary:
        ident = _extraction_id(item)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _query_tokens(query: str) -> list[str]:
    import re

    q = (query or "").strip().lower()
    stop = {
        "in", "on", "the", "a", "an", "to", "for", "of", "we", "how", "do",
        "did", "is", "are", "was", "what", "which", "when", "with", "from",
        "that", "this", "our", "and", "or", "not", "only", "get", "got",
        "should", "must", "can", "use", "using", "used", "vs", "versus",
    }
    return [
        t
        for t in re.findall(r"[a-z0-9][a-z0-9:._/-]{1,}", q)
        if len(t) >= 2 and t not in stop
    ]


def _token_weight(tok: str) -> float:
    """Heavier weight for identifiers (env vars, model tags, hostnames, dims)."""
    w = 1.0
    if any(c in tok for c in ":_/.-"):
        w += 2.0  # qwen3-embedding:0.6b, TOTAL_RECALL_VEC, web-01
    if len(tok) >= 10:
        w += 0.75
    elif len(tok) >= 6:
        w += 0.35
    # ALL_CAPS-ish env tokens
    if tok.isupper() or (tok.isupper() is False and tok == tok.upper() and "_" in tok):
        w += 0.5
    if any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok):
        w += 0.5  # 0.6b, 4096, 2b mixed
    return w


def _token_coverage(query: str, content: str) -> float:
    """Weighted fraction of significant query tokens present in content (0..1)."""
    c = (content or "").lower()
    toks = _query_tokens(query)
    if not toks or not c:
        return 0.0
    num = 0.0
    den = 0.0
    for t in toks:
        w = _token_weight(t)
        den += w
        if t in c:
            num += w
    return num / den if den else 0.0


def _identifier_centrality(query: str, content: str) -> float:
    """Prefer docs where query identifiers appear early / as primary subject.

    Fixes cross-seed theft: ``qwen3-embedding:0.6b`` query ranking a dim-fact
    that merely *mentions* the tag over the model-tag decision itself.
    """
    import re

    c = (content or "").lower()
    q = (query or "").strip().lower()
    if not c or not q:
        return 0.0
    idents = re.findall(
        r"[a-z0-9]+(?:[._:-][a-z0-9]+)+|[a-z]*_[a-z0-9_]+|[a-z]+-\d+",
        q,
    )
    if not idents:
        # single short id host/env without punctuation
        toks = _query_tokens(query)
        idents = [t for t in toks if _token_weight(t) >= 1.5] or toks[:1]
    best = 0.0
    for ident in idents:
        pos = c.find(ident)
        if pos < 0:
            continue
        # Earlier mention + shorter focused doc wins ties among exact matches
        pos_s = 1.0 - min(pos, 160) / 160.0
        len_s = 1.0 / (1.0 + len(c) / 180.0)
        # Bonus if ident appears in first clause (before first period)
        first = c.split(".", 1)[0]
        head_s = 1.0 if ident in first else 0.3
        best = max(best, 0.4 * pos_s + 0.3 * len_s + 0.3 * head_s)
    return best


def _hit_rank_score(item: Any, query: str, dense_rank: int | None, fts_rank: int | None) -> float:
    """Higher is better. Blends cosine, weighted lexical coverage, identifiers.

    Adversarial 10×: related product facts share vocabulary — identifier
    centrality + weighted coverage beat pure cosine on symbol/env/model queries.
    """
    content = _hit_content(item)
    cov = _token_coverage(query, content)
    phrase = 1.0 if _exactish_match(query, content) else 0.0
    ident = _identifier_centrality(query, content)
    dense_s = 0.0 if dense_rank is None else 1.0 / (1.0 + dense_rank)
    fts_s = 0.0 if fts_rank is None else 1.0 / (1.0 + fts_rank)
    sim = 0.0
    dist = getattr(item, "cosine_distance", None)
    if isinstance(dist, (int, float)):
        sim = max(0.0, 1.0 - float(dist))
    kind = getattr(item, "kind", None) or (
        item.get("kind") if isinstance(item, dict) else ""
    )
    try:
        from .store import _DENSE_KIND_BOOST

        kind_s = float(_DENSE_KIND_BOOST.get(str(kind or ""), 0.02)) / 0.14
    except Exception:  # noqa: BLE001
        kind_s = 0.0
    return (
        0.28 * sim
        + 0.26 * cov
        + 0.16 * phrase
        + 0.14 * ident
        + 0.08 * dense_s
        + 0.05 * fts_s
        + 0.03 * kind_s
    )


def try_hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    cwd: str | None = None,
    kind: str | None = None,
) -> list[Any] | None:
    """Best-effort hybrid search. ``None`` if vec/embed unavailable or empty query.

    Use this from hooks and MCP tools that previously called FTS-only
    ``search_extractions`` with free-text queries. Callers should fall back to
    FTS when this returns ``None`` or ``[]``.
    """
    if not query or not str(query).strip():
        return None
    try:
        from .embed import Embedder
    except Exception as exc:  # noqa: BLE001
        log.debug("try_hybrid_search: embed import failed: %r", exc)
        return None
    try:
        embedder = Embedder()
        hits = hybrid_search(
            conn,
            str(query).strip(),
            embedder=embedder,
            limit=limit,
            cwd=cwd,
            kind=kind,
        )
        return list(hits) if hits else []
    except Exception as exc:  # noqa: BLE001
        log.debug("try_hybrid_search failed: %r", exc)
        return None


def _dense_primary_merge(
    vec_hits: list[Any],
    fts_hits: list[Any],
    limit: int,
    query: str = "",
) -> list[Any]:
    """Dense rank order first; FTS only appends novel exact-keyword hits.

    Exceptions:
      * FTS exactish top-1 + dense not exactish → FTS primary (symbol queries).
      * Otherwise merge dense-first then **re-rank** the candidate pool by a
        blend of cosine / RRF ranks / token coverage so near-miss domain_facts
        that lack query tokens fall below true hits.
    """
    if (
        query
        and fts_hits
        and vec_hits
        and _exactish_match(query, _hit_content(fts_hits[0]))
        and not _exactish_match(query, _hit_content(vec_hits[0]))
    ):
        return _merge_primary(list(fts_hits), list(vec_hits), limit)

    # Build candidate pool (dense-first order, FTS fill) then coverage re-rank.
    pool = _merge_primary(list(vec_hits), list(fts_hits), max(limit * 3, limit))
    if not query or len(pool) <= 1:
        return pool[:limit]

    dense_rank = {_extraction_id(h): i for i, h in enumerate(vec_hits)}
    fts_rank = {_extraction_id(h): i for i, h in enumerate(fts_hits)}
    scored = sorted(
        pool,
        key=lambda h: _hit_rank_score(
            h,
            query,
            dense_rank.get(_extraction_id(h)),
            fts_rank.get(_extraction_id(h)),
        ),
        reverse=True,
    )
    return scored[:limit]


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
        return _dense_primary_merge(list(vec_hits), list(fts_hits), limit, query=query)

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
