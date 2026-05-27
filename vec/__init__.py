"""Optional vector-embedding layer for total-recall.

This package extends WT-4's SQLite FTS5 index with a dense-retrieval companion
backed by `fastembed` (embedding model) and `sqlite-vec` (vector store). The
two retrievers are combined at query time via Reciprocal Rank Fusion (RRF).

The package is **optional**. The baseline FTS5 index works without it. The
heavy deps (`fastembed`, `sqlite_vec`, `numpy`, model downloads) are imported
*inside* the functions that need them so:

  * `import vec` is cheap and never fails on a stock install.
  * `pip install total-recall` works without pulling ML wheels.
  * `pip install 'total-recall[vec]'` enables the dense path.

If a caller invokes a function that needs the optional deps without having
installed them, we raise a single clear `RuntimeError` with the install hint.
"""

from __future__ import annotations

# Re-export the public surface lazily; keep top-level import featherweight.
__all__ = [
    "Embedder",
    "chunk_for_embedding",
    "apply_vec_schema",
    "upsert_extraction_embedding",
    "backfill_all",
    "vec_search",
    "VecHit",
    "BackfillReport",
    "reciprocal_rank_fusion",
    "hybrid_search",
]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export
    if name in {"Embedder", "chunk_for_embedding"}:
        from . import embed as _embed

        return getattr(_embed, name)
    if name in {
        "apply_vec_schema",
        "upsert_extraction_embedding",
        "backfill_all",
        "vec_search",
        "VecHit",
        "BackfillReport",
    }:
        from . import store as _store

        return getattr(_store, name)
    if name in {"reciprocal_rank_fusion", "hybrid_search"}:
        from . import rrf as _rrf

        return getattr(_rrf, name)
    raise AttributeError(f"module 'vec' has no attribute {name!r}")
