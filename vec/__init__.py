"""Dense-retrieval companion for total-recall.

Embeddings via **local ollama** (default ``qwen3-embedding:0.6b``). Vectors
stored with ``sqlite-vec``. Combined with FTS5 at query time via RRF.

FTS5 works without dense. Dense requires a running ollama daemon with an
embedding-capable model pulled. ``import vec`` stays cheap (lazy sqlite-vec).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .embed import Embedder, chunk_for_embedding
    from .rrf import hybrid_search, reciprocal_rank_fusion
    from .store import (
        BackfillReport,
        VecHit,
        apply_vec_schema,
        backfill_all,
        upsert_extraction_embedding,
        vec_search,
    )

__all__ = [
    "Embedder",
    "chunk_for_embedding",
    "apply_vec_schema",
    "backfill_all",
    "upsert_extraction_embedding",
    "vec_search",
    "hybrid_search",
    "reciprocal_rank_fusion",
    "BackfillReport",
    "VecHit",
]


def __getattr__(name: str):
    if name in {"Embedder", "chunk_for_embedding"}:
        from . import embed as _embed

        return getattr(_embed, name)
    if name in {"hybrid_search", "reciprocal_rank_fusion"}:
        from . import rrf as _rrf

        return getattr(_rrf, name)
    if name in {
        "BackfillReport",
        "VecHit",
        "apply_vec_schema",
        "backfill_all",
        "upsert_extraction_embedding",
        "vec_search",
    }:
        from . import store as _store

        return getattr(_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
