"""Tests for the dense vector layer (ollama embeds + sqlite-vec + RRF).

Hermetic: ollama is mocked. Live daemon not required for unit tests.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _have(modname: str) -> bool:
    try:
        importlib.import_module(modname)
        return True
    except Exception:
        return False


def _have_sqlite_vec() -> bool:
    if not _have("sqlite_vec"):
        return False
    try:
        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("CREATE VIRTUAL TABLE t USING vec0(embedding float[4])")
        conn.close()
        return True
    except Exception:
        return False


HAS_SQLITE_VEC = _have_sqlite_vec()


class TestReciprocalRankFusion:
    def test_empty_input(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        assert reciprocal_rank_fusion([]) == []

    def test_single_ranking_is_passthrough_order(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        items = ["a", "b", "c"]
        fused = reciprocal_rank_fusion([items], k=60)
        assert [item for item, _ in fused] == items
        # Scores strictly decreasing.
        scores = [s for _, s in fused]
        assert scores == sorted(scores, reverse=True)

    def test_two_rankings_share_top_item(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        # 'X' is rank 0 in both lists → must win.
        r1 = ["X", "A", "B"]
        r2 = ["X", "C", "D"]
        fused = reciprocal_rank_fusion([r1, r2], k=60)
        assert fused[0][0] == "X"

        # Hand-verify the score for X: 1/(60+1) + 1/(60+1) = 2/61
        expected_top = 2.0 / 61
        assert fused[0][1] == pytest.approx(expected_top, rel=1e-12)

    def test_dedup_across_rankings(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        r1 = ["A", "B", "C"]
        r2 = ["C", "B", "A"]
        fused = reciprocal_rank_fusion([r1, r2], k=60)
        items = [item for item, _ in fused]
        # All three items appear exactly once.
        assert sorted(items) == ["A", "B", "C"]
        # A: 1/61 + 1/63, B: 2/62, C: 1/63 + 1/61 → A and C tie above B.
        # Verify the score relationships numerically.
        score = dict(fused)
        assert score["A"] == pytest.approx(score["C"], rel=1e-12)
        assert score["A"] > score["B"]

    def test_k_increases_dampening(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        # Use an *asymmetric* setup so the gap between A and B is non-zero.
        # A is rank 0 in both lists, B is rank 1 in both lists.
        r1 = ["A", "B"]
        r2 = ["A", "B"]
        fused_small_k = dict(reciprocal_rank_fusion([r1, r2], k=1))
        fused_large_k = dict(reciprocal_rank_fusion([r1, r2], k=1000))

        # With small k the rank-0 contribution (1/(k+1)) dominates rank-1
        # (1/(k+2)) by a much larger relative margin than with large k.
        gap_small = fused_small_k["A"] - fused_small_k["B"]
        gap_large = fused_large_k["A"] - fused_large_k["B"]
        assert gap_small > 0
        assert gap_large > 0
        assert gap_large < gap_small

    def test_custom_key_function(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        r1 = [{"id": 1, "src": "fts"}, {"id": 2, "src": "fts"}]
        r2 = [{"id": 2, "src": "vec"}, {"id": 1, "src": "vec"}]
        fused = reciprocal_rank_fusion([r1, r2], k=60, key=lambda d: d["id"])
        ids = [item["id"] for item, _ in fused]
        assert sorted(ids) == [1, 2]
        # id=2 appears at rank 1 then rank 0 → ties go to id=2 (slightly higher
        # because rank-0 contribution is bigger than rank-1's).
        # Verify the ordering by computed score:
        score_by_id = {item["id"]: score for item, score in fused}
        assert score_by_id[2] >= score_by_id[1]

    def test_empty_inner_ranking_is_skipped(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        fused = reciprocal_rank_fusion([["a", "b"], []], k=60)
        assert [item for item, _ in fused] == ["a", "b"]

    def test_returns_canonical_best_item(self) -> None:
        """When two rankings disagree on the *payload* for the same key, the
        canonical (best-ranked) one is what gets returned."""
        from vec.rrf import reciprocal_rank_fusion

        r1 = [{"id": 1, "label": "from-fts"}]
        r2 = [{"id": 1, "label": "from-vec"}]
        # Same rank in both; the first ranking's item wins by tie-break.
        fused = reciprocal_rank_fusion([r1, r2], k=60, key=lambda d: d["id"])
        assert fused[0][0]["label"] == "from-fts"


# ----------------------------------------------------------------------------
# chunk_for_embedding tests
# ----------------------------------------------------------------------------


class TestChunkForEmbedding:
    def test_empty_input(self) -> None:
        from vec.embed import chunk_for_embedding

        assert chunk_for_embedding("") == []
        assert chunk_for_embedding("   \n  ") == []

    def test_short_text_returns_one_chunk(self) -> None:
        from vec.embed import chunk_for_embedding

        chunks = chunk_for_embedding("Hello world. How are you?")
        assert len(chunks) == 1
        assert "Hello world" in chunks[0]
        assert "How are you" in chunks[0]

    def test_paragraph_boundary_split(self) -> None:
        from vec.embed import chunk_for_embedding

        # Force a split by using a tiny budget.
        text = "First paragraph here.\n\nSecond paragraph here.\n\nThird here."
        chunks = chunk_for_embedding(text, max_tokens=5, overlap=0)
        assert len(chunks) >= 2

    def test_hard_split_when_sentence_exceeds_budget(self) -> None:
        from vec.embed import chunk_for_embedding

        long_sentence = "a" * 1000  # no sentence terminator at all
        chunks = chunk_for_embedding(long_sentence, max_tokens=50, overlap=0)
        # 50 tokens * 4 chars/token = 200 chars/chunk → at least 5 chunks.
        assert len(chunks) >= 5
        # Reassembling must give back the original.
        assert "".join(chunks) == long_sentence

    def test_overlap_carries_tail_into_next_chunk(self) -> None:
        from vec.embed import chunk_for_embedding

        sentences = [f"Sentence number {i} ends here." for i in range(20)]
        text = " ".join(sentences)
        no_overlap = chunk_for_embedding(text, max_tokens=20, overlap=0)
        with_overlap = chunk_for_embedding(text, max_tokens=20, overlap=10)

        assert len(no_overlap) >= 2
        assert len(with_overlap) >= 2
        # Overlap should make total emitted characters >= no-overlap.
        assert sum(len(c) for c in with_overlap) >= sum(len(c) for c in no_overlap)

    def test_no_duplicate_consecutive_chunks(self) -> None:
        from vec.embed import chunk_for_embedding

        chunks = chunk_for_embedding("Short.", max_tokens=400, overlap=50)
        # No back-to-back identical chunks even though the overlap window
        # would otherwise replicate this short text.
        for a, b in zip(chunks, chunks[1:], strict=False):
            assert a != b


# ----------------------------------------------------------------------------

class TestOllamaEmbedder:
    _MODELS = [
        {"name": "granite-embedding:30m", "size": 30_000_000, "capabilities": ["embedding"]},
        {"name": "qwen3-embedding:0.6b", "size": 600_000_000, "capabilities": ["embedding"]},
        {"name": "qwen3.5:2b", "size": 2_000_000_000, "capabilities": ["completion"]},
    ]

    def test_import_vec_is_cheap(self) -> None:
        import vec
        import vec.embed  # noqa: F401

        assert hasattr(vec, "Embedder")

    def test_pick_prefers_qwen3_0_6b(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        assert embed._pick_ollama_embed_model("http://x", None) == "qwen3-embedding:0.6b"

    def test_pick_respects_want(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        assert (
            embed._pick_ollama_embed_model("http://x", "granite-embedding:30m")
            == "granite-embedding:30m"
        )

    def test_pick_want_latest_suffix(self, monkeypatch) -> None:
        from vec import embed

        models = [{"name": "nomic-embed-text:latest", "size": 1, "capabilities": ["embedding"]}]
        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: models)
        assert (
            embed._pick_ollama_embed_model("http://x", "nomic-embed-text")
            == "nomic-embed-text:latest"
        )

    def test_pick_ignores_chat_models(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: [self._MODELS[2]])
        assert embed._pick_ollama_embed_model("http://x", None) is None

    def test_pick_unreachable(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: None)
        assert embed._pick_ollama_embed_model("http://x", None) is None

    def test_pick_want_missing_raises(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        with pytest.raises(RuntimeError, match="not pulled"):
            embed._pick_ollama_embed_model("http://x", "does-not-exist")

    def test_legacy_hf_model_id_raises_migration(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        with pytest.raises(RuntimeError, match="legacy fastembed"):
            embed._pick_ollama_embed_model("http://x", "Alibaba-NLP/gte-modernbert-base")

    def test_embed_uses_ollama(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        monkeypatch.setattr(
            embed,
            "_ollama_embed",
            lambda base_url, model, texts: [[0.1] * 8 for _ in texts],
        )
        e = embed.Embedder()
        vecs = e.embed(["hi"])
        assert e.backend == "ollama"
        assert e.model == "qwen3-embedding:0.6b"
        assert vecs == [[0.1] * 8]
        assert "Instruct:" in e._query_prefix

    def test_embed_as_query_prefixes_qwen3(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        seen: list[list[str]] = []

        def _cap(base_url, model, texts):
            seen.append(list(texts))
            return [[0.0] for _ in texts]

        monkeypatch.setattr(embed, "_ollama_embed", _cap)
        e = embed.Embedder()
        e.embed(["nginx rate limit"], as_query=True)
        assert seen[-1][0].startswith("Instruct:")
        assert seen[-1][0].endswith("nginx rate limit")

    def test_embed_docs_raw_for_qwen3(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        seen: list[list[str]] = []

        def _cap(base_url, model, texts):
            seen.append(list(texts))
            return [[0.0] for _ in texts]

        monkeypatch.setattr(embed, "_ollama_embed", _cap)
        e = embed.Embedder()
        e.embed(["passage about nginx"])
        assert seen[-1] == ["passage about nginx"]

    def test_no_daemon_raises(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: None)
        e = embed.Embedder()
        with pytest.raises(RuntimeError, match="No embedding-capable|ollama"):
            e.embed(["hi"])

    def test_empty_input(self, monkeypatch) -> None:
        from vec import embed

        monkeypatch.setattr(embed, "_ollama_list_models", lambda base_url: self._MODELS)
        e = embed.Embedder()
        assert e.embed([]) == []

    def test_known_dim_qwen3(self) -> None:
        from vec.embed import _KNOWN_DIMS, RECOMMENDED_OLLAMA_EMBED

        assert RECOMMENDED_OLLAMA_EMBED == "qwen3-embedding:0.6b"
        assert _KNOWN_DIMS["qwen3-embedding:0.6b"] == 1024


# ----------------------------------------------------------------------------
# Fake embedder + sqlite-vec incremental (no ollama)
# ----------------------------------------------------------------------------


class _FakeEmbedder:
    model = "fake"
    backend = "fake"

    def dim(self) -> int:
        return 8

    def identity(self) -> str:
        return f"{self.backend}:{self.model}"

    def embed(self, texts, as_query: bool = False):
        return [[float(len(t) % 7)] + [0.1] * 7 for t in texts]


class TestIncrementalVecBackfill:
    @pytest.mark.skipif(not HAS_SQLITE_VEC, reason="sqlite_vec required")
    def test_backfill_only_touches_new_extractions(self, tmp_path: Path) -> None:
        from vec.store import apply_vec_schema, backfill_all

        db_path = tmp_path / "index.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE extractions(
                id INTEGER PRIMARY KEY,
                content TEXT, cwd TEXT, ts TEXT, kind TEXT
            );
            CREATE VIRTUAL TABLE extractions_fts USING fts5(content);
            """
        )
        rows = [
            (1, "nginx rate limiting config", "/a", "2025-01-01", "decision"),
            (2, "chocolate cake recipe details", "/b", "2025-01-02", "note"),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO extractions(id, content, cwd, ts, kind) VALUES (?,?,?,?,?)", r
            )
            conn.execute("INSERT INTO extractions_fts(rowid, content) VALUES (?,?)", (r[0], r[1]))
        conn.commit()

        emb = _FakeEmbedder()
        apply_vec_schema(conn, dim=emb.dim(), model=emb.model, backend=emb.backend)
        first = backfill_all(conn, embedder=emb)
        assert first.extractions_embedded == 2
        conn.commit()

        conn.execute(
            "INSERT INTO extractions(id, content, cwd, ts, kind) VALUES (?,?,?,?,?)",
            (3, "postgres row level security", "/c", "2025-01-03", "decision"),
        )
        conn.execute(
            "INSERT INTO extractions_fts(rowid, content) VALUES (?,?)",
            (3, "postgres row level security"),
        )
        conn.commit()

        second = backfill_all(conn, embedder=_FakeEmbedder())
        assert second.extractions_embedded == 1
        n = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
        assert n >= 3


class TestWeightedRRF:
    def test_weights_boost_second_ranking(self) -> None:
        from vec.rrf import reciprocal_rank_fusion

        # Equal weight: 'A' wins (rank0 in both). Dense-heavy: 'B' can win.
        fts = ["A", "B"]
        dense = ["B", "A"]
        eq = reciprocal_rank_fusion([fts, dense], k=60)
        assert eq[0][0] in ("A", "B")
        heavy = reciprocal_rank_fusion([fts, dense], k=60, weights=[1.0, 10.0])
        assert heavy[0][0] == "B"

    def test_dense_primary_merge_keeps_dense_order(self) -> None:
        from vec.rrf import _dense_primary_merge

        class Hit:
            def __init__(self, eid: int, content: str) -> None:
                self.extraction_id = eid
                self.content = content

        dense = [Hit(2, "dense-first"), Hit(1, "dense-second")]
        fts = [Hit(9, "fts-only"), Hit(1, "fts-dup")]
        out = _dense_primary_merge(dense, fts, limit=5)
        assert [h.content for h in out] == ["dense-first", "dense-second", "fts-only"]


class TestHybridSoftFail:
    def test_hybrid_without_vec_tables_returns_fts_only(self, tmp_path: Path) -> None:
        """No dense tables → FTS-only hybrid, not crash."""
        from vec.rrf import hybrid_search

        db = tmp_path / "i.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE extractions(
                id INTEGER PRIMARY KEY, content TEXT, cwd TEXT, ts TEXT, kind TEXT
            );
            CREATE VIRTUAL TABLE extractions_fts USING fts5(content);
            INSERT INTO extractions VALUES (1, 'nginx rate limit', '/a', '2025-01-01', 'decision');
            INSERT INTO extractions_fts(rowid, content) VALUES (1, 'nginx rate limit');
            """
        )
        conn.commit()
        hits = hybrid_search(conn, "nginx", embedder=None, limit=5)
        assert hits
