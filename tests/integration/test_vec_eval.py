"""Vec hybrid vs FTS5-only recall quality eval (v1.6.0).

This is the go/no-go measurement for the v2.0 vec-by-default decision the PM
council deferred: does dense hybrid retrieval (RRF of FTS5 + sqlite-vec/fastembed)
actually beat FTS5-only on *paraphrase* queries — the case where the query and
the stored extraction share meaning but few exact tokens?

It builds a small labeled corpus where each query is phrased DIFFERENTLY from
its target extraction (so exact-term FTS5 is challenged), embeds the corpus,
then measures precision@5 (target in top-5) for both retrievers.

Auto-SKIPS when fastembed / sqlite-vec are absent, so the default suite stays
green. The assertion is intentionally weak — hybrid must NOT be *worse* than
FTS5 on this set (>= FTS5 P@k). The strong ">= +5pp" promotion gate is recorded
in the printed scorecard for the human v2.0 decision, not enforced here (a
20-pair synthetic set is too small to hard-gate a release on).

Run with output:  TOTAL_RECALL_VEC_EVAL=1 pytest tests/integration/test_vec_eval.py -s
"""
from __future__ import annotations

import sqlite3

import pytest

# Skip the whole module unless both vec deps import.
fastembed = pytest.importorskip("fastembed", reason="vec extra not installed")
sqlite_vec = pytest.importorskip("sqlite_vec", reason="vec extra not installed")

from index.db import connect  # noqa: E402

# Labeled paraphrase set: (query, target_content). The query deliberately uses
# different words from the target — synonyms, generalizations, role-words — so a
# pure-keyword retriever is stressed and a semantic one has a chance to win.
_CORPUS: list[tuple[str, str]] = [
    ("postgres driver to use", "decided to standardize on asyncpg for all database access"),
    ("how should I run background jobs", "use a task queue with celery workers, not threads"),
    ("container orchestration choice", "we run everything on kubernetes in production"),
    ("secrets management approach", "store credentials in vault, never in env files"),
    ("frontend build tooling", "migrated the web app bundler to vite from webpack"),
    ("logging destination", "ship structured logs to loki via promtail"),
    ("api authentication method", "all endpoints require a bearer token from the auth service"),
    ("how to format python code", "run ruff for linting and formatting, drop black"),
    ("database migration tool", "schema changes go through alembic revisions"),
    ("caching layer", "redis fronts the read-heavy queries"),
    ("error monitoring service", "exceptions are reported to sentry in prod"),
    ("ci pipeline runner", "github actions builds and tests every push"),
    ("message bus technology", "services talk over nats jetstream, not rabbitmq"),
    ("python dependency manager", "use uv for installs and lockfiles, not pip"),
    ("how do we deploy", "argocd syncs manifests to the cluster on merge"),
]

# Distractors: plausible but NOT the target for any query, to make top-5 mean
# something on a 15-target set.
_DISTRACTORS: list[str] = [
    "the standup is at 10am daily",
    "the office wifi password rotates monthly",
    "lunch is catered on fridays",
    "the logo uses the brand teal #1aa",
    "remember to expense the conference tickets",
]


def _build_corpus(conn: sqlite3.Connection) -> None:
    ts = 1_700_000_000
    for i, (_q, content) in enumerate(_CORPUS):
        conn.execute(
            "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
            "score, scope) VALUES (?,?,?,?,?,?,?,?)",
            ("decision", content, "s", "/proj/eval", ts + i, f"t{i}", 0.7, "project"),
        )
    for j, d in enumerate(_DISTRACTORS):
        conn.execute(
            "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
            "score, scope) VALUES (?,?,?,?,?,?,?,?)",
            ("domain_fact", d, "s", "/proj/eval", ts + 100 + j, f"d{j}", 0.5, "project"),
        )
    conn.commit()


def _precision_at_k(hits: list, target: str, k: int) -> float:
    top = [getattr(h, "content", None) for h in hits[:k]]
    return 1.0 if target in top else 0.0


def test_hybrid_not_worse_than_fts5_on_paraphrases(tmp_path, capsys) -> None:
    from vec.rrf import hybrid_search
    from vec.store import apply_vec_schema, backfill_all
    from vec.embed import Embedder

    db = tmp_path / "eval.db"
    conn = connect(db)
    try:
        _build_corpus(conn)
        apply_vec_schema(conn)
        embedder = Embedder()  # loads fastembed CPU model on first embed
        report = backfill_all(conn, embedder=embedder, only_kinds=None)
        assert report.extractions_embedded > 0, "nothing got embedded"

        k = 5
        fts_p = 0.0
        hyb_p = 0.0
        for query, target in _CORPUS:
            fts_hits = hybrid_search(conn, query, embedder=None, limit=k, cwd="/proj/eval")
            hyb_hits = hybrid_search(conn, query, embedder=embedder, limit=k, cwd="/proj/eval")
            fts_p += _precision_at_k(fts_hits, target, k)
            hyb_p += _precision_at_k(hyb_hits, target, k)
        n = len(_CORPUS)
        fts_p /= n
        hyb_p /= n

        with capsys.disabled():
            print(
                f"\n=== VEC EVAL (n={n}, k={k}) ===\n"
                f"  FTS5-only  P@{k}: {fts_p:.3f}\n"
                f"  Hybrid     P@{k}: {hyb_p:.3f}\n"
                f"  delta:           {hyb_p - fts_p:+.3f}\n"
                f"  v2.0 promotion gate (>= +0.05): "
                f"{'PASS' if hyb_p - fts_p >= 0.05 else 'NOT MET'}\n"
            )

        # Weak gate: hybrid must not REGRESS recall vs FTS5 on paraphrases.
        # The strong +5pp promotion gate is reported above for the human v2.0
        # decision; a 15-pair set is too small to hard-fail a build on.
        assert hyb_p >= fts_p, (
            f"hybrid regressed vs FTS5: hybrid={hyb_p:.3f} fts={fts_p:.3f}"
        )
    finally:
        conn.close()
