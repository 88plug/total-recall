"""Tests for the optional vector layer.

These tests are written to pass cleanly even when neither `fastembed` nor
`sqlite_vec` is installed — the integration test that exercises both is
auto-skipped in that case.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

# Make the repo root importable without requiring an install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ----------------------------------------------------------------------------
# Optional-dep detection
# ----------------------------------------------------------------------------


def _have(modname: str) -> bool:
    try:
        importlib.import_module(modname)
        return True
    except Exception:
        return False


HAS_FASTEMBED = _have("fastembed")
HAS_SQLITE_VEC = _have("sqlite_vec")


# ----------------------------------------------------------------------------
# RRF unit tests
# ----------------------------------------------------------------------------


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
        for a, b in zip(chunks, chunks[1:]):
            assert a != b


# ----------------------------------------------------------------------------
# Lazy-import test: importing vec / vec.embed must NOT pull fastembed.
# ----------------------------------------------------------------------------


class TestLazyImports:
    def test_import_vec_does_not_load_fastembed(self) -> None:
        # Drop any cached fastembed first.
        for mod in list(sys.modules):
            if mod == "fastembed" or mod.startswith("fastembed."):
                del sys.modules[mod]
        # Importing the package + the submodule must not pull fastembed.
        importlib.import_module("vec")
        importlib.import_module("vec.embed")
        assert "fastembed" not in sys.modules

    def test_constructing_embedder_does_not_load_fastembed(self) -> None:
        for mod in list(sys.modules):
            if mod == "fastembed" or mod.startswith("fastembed."):
                del sys.modules[mod]
        from vec.embed import Embedder

        # __init__ must be lazy. .embed() is what actually triggers load.
        _ = Embedder(model="BAAI/bge-small-en-v1.5")
        assert "fastembed" not in sys.modules

    def test_known_dim_available_without_loading_model(self) -> None:
        from vec.embed import Embedder

        emb = Embedder(model="BAAI/bge-small-en-v1.5")
        # 384-dim is known a priori, so .dim() should not need to embed.
        assert emb.dim() == 384

    def test_embedder_without_fastembed_raises_clearly(self) -> None:
        if HAS_FASTEMBED:
            pytest.skip("fastembed is installed — error path not reachable here.")
        from vec.embed import Embedder

        emb = Embedder()
        with pytest.raises(RuntimeError) as excinfo:
            emb.embed(["hi"])
        msg = str(excinfo.value).lower()
        assert "fastembed" in msg
        assert "vec" in msg  # mentions the extra


# ----------------------------------------------------------------------------
# GTE / granite custom-model port (registry + tokenizer clamp) — network-free
# ----------------------------------------------------------------------------


class TestGteCustomModels:
    def test_registry_wiring(self) -> None:
        from vec.embed import _KNOWN_DIMS, _CUSTOM_MODELS

        assert _KNOWN_DIMS["Alibaba-NLP/gte-modernbert-base"] == 768
        assert _KNOWN_DIMS["onnx-community/granite-embedding-small-english-r2"] == 384
        for key, expected in (
            ("Alibaba-NLP/gte-modernbert-base", 768),
            ("onnx-community/granite-embedding-small-english-r2", 384),
        ):
            assert key in _CUSTOM_MODELS
            assert _CUSTOM_MODELS[key]["dim"] == expected

    def test_clamp_rewrites_huge_sentinel(self, tmp_path: Path) -> None:
        import json
        from vec.embed import _clamp_tokenizer_max_length

        tc = tmp_path / "tokenizer_config.json"
        tc.write_text(json.dumps({
            "model_max_length": 1000000000000000000000000000000,
            "model_type": "modernbert",
        }))
        _clamp_tokenizer_max_length(tmp_path)
        d = json.loads(tc.read_text())
        assert d["model_max_length"] == 8192
        assert d["model_type"] == "modernbert"  # unrelated key preserved

    def test_clamp_leaves_sane_value_untouched(self, tmp_path: Path) -> None:
        import json
        from vec.embed import _clamp_tokenizer_max_length

        tc = tmp_path / "tokenizer_config.json"
        tc.write_text(json.dumps({"model_max_length": 8192}))
        _clamp_tokenizer_max_length(tmp_path)
        assert json.loads(tc.read_text())["model_max_length"] == 8192

    def test_clamp_missing_file_is_noop(self, tmp_path: Path) -> None:
        from vec.embed import _clamp_tokenizer_max_length

        # Empty dir: must not raise.
        assert _clamp_tokenizer_max_length(tmp_path) is None

    @pytest.mark.skipif(not HAS_FASTEMBED, reason="fastembed required")
    def test_register_custom_model_idempotent(self) -> None:
        from vec.embed import _register_custom_model

        # Metadata-only registration; second call swallows the 'already' ValueError.
        _register_custom_model("Alibaba-NLP/gte-modernbert-base")
        _register_custom_model("Alibaba-NLP/gte-modernbert-base")


# ----------------------------------------------------------------------------
# Custom-model port regression net (clamp edge cases + offline registration)
# ----------------------------------------------------------------------------


class TestCustomModelPorts:
    def test_clamp_rewrites_huge_model_max_length(self, tmp_path: Path) -> None:
        import json
        from vec.embed import _clamp_tokenizer_max_length

        tc = tmp_path / "tokenizer_config.json"
        tc.write_text(json.dumps({
            "model_max_length": 1000000000000000019884624838656,
            "max_length": 2000000000000,
            "model_type": "modernbert",
        }))
        _clamp_tokenizer_max_length(tmp_path)
        d = json.loads(tc.read_text())
        assert d["model_max_length"] == 8192
        assert d["max_length"] == 8192
        assert d["model_type"] == "modernbert"

    def test_clamp_noop_when_already_sane(self, tmp_path: Path) -> None:
        import json
        from vec.embed import _clamp_tokenizer_max_length

        tc = tmp_path / "tokenizer_config.json"
        tc.write_text(json.dumps({"model_max_length": 8192}))
        _clamp_tokenizer_max_length(tmp_path)
        assert json.loads(tc.read_text())["model_max_length"] == 8192

    def test_clamp_missing_file_is_silent(self, tmp_path: Path) -> None:
        from vec.embed import _clamp_tokenizer_max_length

        assert _clamp_tokenizer_max_length(tmp_path) is None

    def test_clamp_malformed_json_is_silent(self, tmp_path: Path) -> None:
        from vec.embed import _clamp_tokenizer_max_length

        (tmp_path / "tokenizer_config.json").write_text("{ not json")
        # Must not raise.
        assert _clamp_tokenizer_max_length(tmp_path) is None

    def test_custom_models_have_known_dims(self) -> None:
        from vec.embed import _KNOWN_DIMS, _CUSTOM_MODELS

        assert _KNOWN_DIMS["Alibaba-NLP/gte-modernbert-base"] == 768
        assert _KNOWN_DIMS["onnx-community/granite-embedding-small-english-r2"] == 384
        assert set(_CUSTOM_MODELS) <= set(_KNOWN_DIMS)
        for k in _CUSTOM_MODELS:
            assert _CUSTOM_MODELS[k]["dim"] == _KNOWN_DIMS[k]

    def test_known_dim_for_custom_models_without_loading(self) -> None:
        for mod in list(sys.modules):
            if mod == "fastembed" or mod.startswith("fastembed."):
                del sys.modules[mod]
        from vec.embed import Embedder

        for name, expected in (
            ("Alibaba-NLP/gte-modernbert-base", 768),
            ("onnx-community/granite-embedding-small-english-r2", 384),
        ):
            emb = Embedder(model=name)
            assert emb.dim() == expected
            assert emb._impl is None
        assert "fastembed" not in sys.modules

    @pytest.mark.skipif(not HAS_FASTEMBED, reason="fastembed required")
    def test_register_custom_model_offline_and_idempotent(self) -> None:
        from vec.embed import _register_custom_model

        _register_custom_model("Alibaba-NLP/gte-modernbert-base")
        _register_custom_model("Alibaba-NLP/gte-modernbert-base")  # 'already' swallowed
        # Unknown model is a no-op (spec is None -> early return).
        assert _register_custom_model("not-a-custom-model") is None

    @pytest.mark.skipif(not HAS_FASTEMBED, reason="fastembed required")
    def test_install_tokenizer_clamp_idempotent(self) -> None:
        from vec.embed import _install_tokenizer_clamp
        from fastembed.text import onnx_text_model as otm

        _install_tokenizer_clamp()
        assert getattr(otm, "_tr_maxlen_clamp", False) is True
        wrapped = otm.load_tokenizer
        _install_tokenizer_clamp()  # guard must prevent a second wrap
        assert otm.load_tokenizer is wrapped


# ----------------------------------------------------------------------------
# Opt-in, model-aware query prefix (bge-class only) — network-free
# ----------------------------------------------------------------------------


class TestQueryPrefix:
    _BGE = "Represent this sentence for searching relevant passages: "

    def test_prefix_resolved_per_model(self) -> None:
        from vec.embed import Embedder

        assert Embedder(model="BAAI/bge-small-en-v1.5")._query_prefix == self._BGE
        for name in (
            "Alibaba-NLP/gte-modernbert-base",
            "onnx-community/granite-embedding-small-english-r2",
            "nomic-ai/nomic-embed-text-v1.5",
        ):
            assert Embedder(model=name)._query_prefix == ""

    def _stub(self, monkeypatch, model):
        from vec.embed import Embedder

        emb = Embedder(model=model)
        seen: list[list[str]] = []

        class _Impl:
            def embed(self, texts):
                seen.append(list(texts))
                return [[0.0] for _ in texts]

        monkeypatch.setattr(emb, "_load", lambda: None)
        emb._impl = _Impl()
        return emb, seen

    def test_bge_as_query_prepends(self, monkeypatch) -> None:
        for mod in list(sys.modules):
            if mod == "fastembed" or mod.startswith("fastembed."):
                del sys.modules[mod]
        emb, seen = self._stub(monkeypatch, "BAAI/bge-small-en-v1.5")
        emb.embed(["nginx", "postgres"], as_query=True)
        assert seen[-1] == [self._BGE + "nginx", self._BGE + "postgres"]
        assert "fastembed" not in sys.modules

    def test_bge_default_is_verbatim(self, monkeypatch) -> None:
        emb, seen = self._stub(monkeypatch, "BAAI/bge-small-en-v1.5")
        emb.embed(["nginx", "postgres"])  # as_query defaults False
        assert seen[-1] == ["nginx", "postgres"]

    def test_gte_as_query_is_verbatim(self, monkeypatch) -> None:
        emb, seen = self._stub(monkeypatch, "Alibaba-NLP/gte-modernbert-base")
        emb.embed(["nginx"], as_query=True)  # empty prefix -> no-op
        assert seen[-1] == ["nginx"]

    def test_empty_input_as_query(self) -> None:
        from vec.embed import Embedder

        assert Embedder(model="BAAI/bge-small-en-v1.5").embed([], as_query=True) == []


# ----------------------------------------------------------------------------
# Incremental vec backfill on the index tick (cmd_index -> _backfill_vectors)
# ----------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic embedder: each text -> a small fixed-dim vector. No model."""

    model = "fake"

    def __init__(self, *a, **k) -> None:
        pass

    def dim(self) -> int:
        return 8

    def embed(self, texts, as_query: bool = False):
        # Vary the first component by text length so vectors aren't identical.
        return [[float(len(t) % 7)] + [0.1] * 7 for t in texts]


class TestIncrementalVecBackfill:
    @pytest.mark.skipif(
        not HAS_SQLITE_VEC, reason="sqlite_vec required for vec schema"
    )
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
        apply_vec_schema(conn, dim=emb.dim())
        first = backfill_all(conn, embedder=emb)
        assert first.extractions_embedded == 2
        conn.commit()

        # New extraction arrives (what _commit_parsed does on an incremental tick).
        conn.execute(
            "INSERT INTO extractions(id, content, cwd, ts, kind) VALUES (?,?,?,?,?)",
            (3, "postgres row level security", "/c", "2025-01-03", "decision"),
        )
        conn.execute("INSERT INTO extractions_fts(rowid, content) VALUES (?,?)", (3, "postgres row level security"))
        conn.commit()

        second = backfill_all(conn, embedder=_FakeEmbedder())
        # Incremental WHERE => only the unembedded row is seen and embedded.
        assert second.extractions_seen == 1
        assert second.extractions_embedded == 1
        n = conn.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE extraction_id = 3"
        ).fetchone()[0]
        assert n >= 1
        conn.close()

    def test_index_cmd_calls_backfill_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types as _types
        from unittest.mock import Mock

        # Stub the WT-4 index.ingest module so the CLI doesn't depend on it.
        fake_pkg = _types.ModuleType("index")
        fake_ingest = _types.ModuleType("index.ingest")
        fake_ingest.ingest_all = lambda **kw: []  # type: ignore[attr-defined]
        fake_pkg.ingest = fake_ingest  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "index", fake_pkg)
        monkeypatch.setitem(sys.modules, "index.ingest", fake_ingest)

        import total_recall.cmd_index as cmd_index
        from total_recall.__main__ import cli
        from click.testing import CliRunner

        backfill_mock = Mock()
        monkeypatch.setattr(cmd_index, "_backfill_vectors", backfill_mock)
        monkeypatch.delenv("TOTAL_RECALL_VEC", raising=False)

        db = tmp_path / "idx.db"
        runner = CliRunner()

        # Non-dry-run: backfill IS invoked once with the db path.
        result = runner.invoke(cli, ["--db", str(db), "index"])
        assert result.exit_code == 0, result.output
        assert backfill_mock.call_count == 1
        assert backfill_mock.call_args.args[0] == str(db)

        # Dry-run: backfill is NOT invoked.
        backfill_mock.reset_mock()
        result = runner.invoke(cli, ["--db", str(db), "index", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert backfill_mock.call_count == 0

    def test_index_cmd_backfill_respects_env_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types as _types
        from unittest.mock import Mock

        fake_pkg = _types.ModuleType("index")
        fake_ingest = _types.ModuleType("index.ingest")
        fake_ingest.ingest_all = lambda **kw: []  # type: ignore[attr-defined]
        fake_pkg.ingest = fake_ingest  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "index", fake_pkg)
        monkeypatch.setitem(sys.modules, "index.ingest", fake_ingest)

        from total_recall.__main__ import cli
        from click.testing import CliRunner

        # With the gate off, the real _backfill_vectors short-circuits before
        # ever reaching backfill_all (stub it to fail the test if reached).
        called = Mock()
        monkeypatch.setattr("vec.store.backfill_all", called)
        monkeypatch.setenv("TOTAL_RECALL_VEC", "0")

        db = tmp_path / "idx.db"
        runner = CliRunner()
        result = runner.invoke(cli, ["--db", str(db), "index"])
        assert result.exit_code == 0, result.output
        assert called.call_count == 0


# ----------------------------------------------------------------------------
# Hybrid search degraded-mode test (no embedder / no sqlite_vec)
# ----------------------------------------------------------------------------


class TestHybridSearchFallback:
    def test_no_embedder_falls_back_gracefully(self, tmp_path: Path) -> None:
        from vec.rrf import hybrid_search

        # An empty DB shouldn't crash hybrid_search.
        conn = sqlite3.connect(":memory:")
        result = hybrid_search(conn, "anything", embedder=None, limit=5)
        assert result == []

    def test_empty_query_returns_empty(self) -> None:
        from vec.rrf import hybrid_search

        conn = sqlite3.connect(":memory:")
        assert hybrid_search(conn, "", embedder=None) == []
        assert hybrid_search(conn, "   ", embedder=None) == []

    def test_fts5_only_path_works_with_inline_fts(self) -> None:
        """When WT-4's index.query isn't importable, the inline FTS fallback
        should still serve results."""
        from vec.rrf import hybrid_search

        conn = sqlite3.connect(":memory:")
        # Build the minimum schema the inline fallback expects.
        conn.executescript(
            """
            CREATE TABLE extractions(
                id INTEGER PRIMARY KEY,
                content TEXT, cwd TEXT, ts TEXT, kind TEXT
            );
            CREATE VIRTUAL TABLE extractions_fts USING fts5(content);
            """
        )
        conn.execute(
            "INSERT INTO extractions(id, content, cwd, ts, kind) "
            "VALUES (1, 'rate limiting nginx config', '/proj/a', '2025-01-01', 'decision')"
        )
        conn.execute("INSERT INTO extractions_fts(rowid, content) VALUES (1, 'rate limiting nginx config')")
        conn.execute(
            "INSERT INTO extractions(id, content, cwd, ts, kind) "
            "VALUES (2, 'unrelated banana smoothie', '/proj/b', '2025-01-02', 'note')"
        )
        conn.execute("INSERT INTO extractions_fts(rowid, content) VALUES (2, 'unrelated banana smoothie')")
        conn.commit()

        hits = hybrid_search(conn, "nginx", embedder=None, limit=5)
        assert len(hits) == 1
        # Inline fallback returns dicts.
        assert hits[0]["extraction_id"] == 1


# ----------------------------------------------------------------------------
# CLI smoke test
# ----------------------------------------------------------------------------


class TestCLI:
    def test_parser_builds(self) -> None:
        from vec.cli import build_parser

        p = build_parser()
        # All three subcommands exist.
        args = p.parse_args(["backfill", "--batch", "32"])
        assert args.cmd == "backfill"
        assert args.batch == 32

        args = p.parse_args(["search", "hello world", "--limit", "3"])
        assert args.cmd == "search"
        assert args.query == "hello world"
        assert args.limit == 3

        args = p.parse_args(["rebuild"])
        assert args.cmd == "rebuild"


# ----------------------------------------------------------------------------
# Integration test (only runs if BOTH optional deps are installed)
# ----------------------------------------------------------------------------


@pytest.mark.skipif(
    not (HAS_FASTEMBED and HAS_SQLITE_VEC),
    reason="fastembed and sqlite_vec must both be installed",
)
class TestIntegration:
    def test_backfill_then_hybrid_search_ranks_correctly(self, tmp_path: Path) -> None:
        from vec.embed import Embedder
        from vec.rrf import hybrid_search
        from vec.store import apply_vec_schema, backfill_all, vec_search

        db_path = tmp_path / "index.db"
        conn = sqlite3.connect(str(db_path))
        # Minimum WT-4-shaped schema.
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
            (1, "Configure nginx rate limiting with limit_req_zone directive.", "/proj/a", "2025-01-01", "decision"),
            (2, "Best chocolate cake recipe with cocoa and butter.", "/proj/b", "2025-01-02", "note"),
            (3, "Use PostgreSQL row-level security for tenant isolation.", "/proj/c", "2025-01-03", "decision"),
        ]
        for r in rows:
            conn.execute("INSERT INTO extractions(id, content, cwd, ts, kind) VALUES (?,?,?,?,?)", r)
            conn.execute("INSERT INTO extractions_fts(rowid, content) VALUES (?,?)", (r[0], r[1]))
        conn.commit()

        embedder = Embedder()
        apply_vec_schema(conn, dim=embedder.dim())
        report = backfill_all(conn, embedder=embedder, batch_size=4)
        assert report.extractions_embedded == 3
        assert report.chunks_written >= 3

        # Pure vec query: "web server traffic shaping" should bring back the
        # nginx row (id=1) even though the literal words don't overlap.
        hits = vec_search(conn, "web server traffic shaping", embedder, limit=3)
        assert hits, "vec_search returned no hits"
        assert hits[0].extraction_id == 1

        # Hybrid query should also rank id=1 first.
        fused = hybrid_search(conn, "rate limiting", embedder=embedder, limit=3)
        assert fused, "hybrid_search returned no hits"
        # Item shape may be VecHit or dict — both expose extraction_id.
        top = fused[0]
        top_id = getattr(top, "extraction_id", None) or top["extraction_id"]
        assert top_id == 1
