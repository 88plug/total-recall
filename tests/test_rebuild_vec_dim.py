"""Regression: cmd_rebuild._backfill_vectors wires the embedder's dim/instance.

Pure-unit: lazily-imported symbols (index.db.connect, vec.store.apply_vec_schema /
backfill_all, vec.embed.Embedder) are monkeypatched, so no fastembed, sqlite_vec,
or model download is needed.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

# Make the repo root importable without requiring an install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class FakeEmbedder:
    model = "fake"

    def __init__(self, *a, **k) -> None:
        pass

    def dim(self) -> int:
        return 768

    def embed(self, texts):
        return [[0.0] * 768 for _ in texts]


def test_backfill_vectors_passes_embedder_dim(monkeypatch) -> None:
    captured: dict = {}

    def fake_apply(conn, *, dim):
        captured["dim"] = dim

    def fake_backfill(conn, embedder=None, **k):
        captured["embedder"] = embedder
        return types.SimpleNamespace(extractions_embedded=0, chunks_written=0)

    monkeypatch.setattr("index.db.connect", lambda p: sqlite3.connect(":memory:"))
    monkeypatch.setattr("vec.store.apply_vec_schema", fake_apply)
    monkeypatch.setattr("vec.store.backfill_all", fake_backfill)
    monkeypatch.setattr("vec.embed.Embedder", FakeEmbedder)
    monkeypatch.delenv("TOTAL_RECALL_VEC", raising=False)

    from total_recall.cmd_rebuild import _backfill_vectors

    _backfill_vectors(":memory:", verbose=False)

    assert captured["dim"] == 768
    assert isinstance(captured["embedder"], FakeEmbedder)


def test_backfill_vectors_skipped_when_disabled(monkeypatch) -> None:
    captured: dict = {}

    def fake_apply(conn, *, dim):
        captured["dim"] = dim

    monkeypatch.setattr("vec.store.apply_vec_schema", fake_apply)
    monkeypatch.setenv("TOTAL_RECALL_VEC", "0")

    from total_recall.cmd_rebuild import _backfill_vectors

    _backfill_vectors(":memory:", verbose=False)

    assert "dim" not in captured
