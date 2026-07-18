"""Vec hybrid vs FTS5-only recall quality eval (v1.6.0).

This is the go/no-go measurement for dense hybrid retrieval (RRF of FTS5 +
sqlite-vec + ollama embeddings) vs FTS5-only on *paraphrase* queries.

Auto-SKIPS when sqlite-vec is absent or ollama has no embedding model.
Requires a live ollama with qwen3-embedding:0.6b (or TOTAL_RECALL_EMBED_MODEL).

Run with output:  TOTAL_RECALL_VEC_EVAL=1 pytest tests/integration/test_vec_eval.py -s
"""

from __future__ import annotations

import sqlite3
import urllib.request

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")


def _ollama_has_embed() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as resp:
            import json

            data = json.loads(resp.read())
        for m in data.get("models") or []:
            if "embedding" in (m.get("capabilities") or []):
                return True
    except Exception:
        return False
    return False


pytestmark = pytest.mark.skipif(
    not _ollama_has_embed(),
    reason="ollama with embedding-capable model required",
)

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
    # hybrid_search/vec_search filter by project_key when cwd is set — must stamp it.
    cwd = "/proj/eval"
    pk = cwd  # plain path → project_key is identity
    ts = 1_700_000_000
    cols = {r[1] for r in conn.execute("PRAGMA table_info(extractions)").fetchall()}
    has_pk = "project_key" in cols
    for i, (_q, content) in enumerate(_CORPUS):
        if has_pk:
            conn.execute(
                "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
                "score, scope, project_key) VALUES (?,?,?,?,?,?,?,?,?)",
                ("decision", content, "s", cwd, ts + i, f"t{i}", 0.7, "project", pk),
            )
        else:
            conn.execute(
                "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
                "score, scope) VALUES (?,?,?,?,?,?,?,?)",
                ("decision", content, "s", cwd, ts + i, f"t{i}", 0.7, "project"),
            )
    for j, d in enumerate(_DISTRACTORS):
        if has_pk:
            conn.execute(
                "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
                "score, scope, project_key) VALUES (?,?,?,?,?,?,?,?,?)",
                ("domain_fact", d, "s", cwd, ts + 100 + j, f"d{j}", 0.5, "project", pk),
            )
        else:
            conn.execute(
                "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
                "score, scope) VALUES (?,?,?,?,?,?,?,?)",
                ("domain_fact", d, "s", cwd, ts + 100 + j, f"d{j}", 0.5, "project"),
            )
    conn.commit()


def _precision_at_k(hits: list, target: str, k: int) -> float:
    top = [getattr(h, "content", None) for h in hits[:k]]
    return 1.0 if target in top else 0.0


def test_hybrid_not_worse_than_fts5_on_paraphrases(tmp_path, capsys) -> None:
    from vec.embed import Embedder
    from vec.rrf import hybrid_search
    from vec.store import apply_vec_schema, backfill_all

    db = tmp_path / "eval.db"
    conn = connect(db)
    try:
        _build_corpus(conn)
        embedder = Embedder()  # ollama qwen3-embedding:0.6b (or EMBED_MODEL)
        apply_vec_schema(
            conn, dim=embedder.dim(), model=embedder.model, backend=embedder.backend
        )
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
        assert hyb_p >= fts_p, f"hybrid regressed vs FTS5: hybrid={hyb_p:.3f} fts={fts_p:.3f}"
    finally:
        conn.close()


def test_rebuild_populates_vec_embeddings(tmp_path, monkeypatch) -> None:
    """End-to-end: `total-recall rebuild` with TOTAL_RECALL_VEC=1 backfills vecs.

    Proves the v2.0 wiring (cmd_rebuild -> apply_vec_schema + backfill_all)
    actually runs and writes chunk_embeddings, on a tiny synthetic corpus so it
    stays fast. LLM refinement is disabled; only the FTS ingest + vec backfill
    paths are exercised.
    """
    import sqlite3

    from total_recall.__main__ import main

    # Minimal synthetic projects root: one cwd-slug dir, one session jsonl with
    # a couple of user turns that the extractors will turn into rows.
    projects = tmp_path / "projects"
    slug = projects / "-proj-vec"
    slug.mkdir(parents=True)
    sess = slug / "11111111-1111-1111-1111-111111111111.jsonl"
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s1",
            "cwd": "/proj/vec",
            "timestamp": "2026-05-01T00:00:00Z",
            "message": {"role": "user", "content": "we decided to use asyncpg for postgres"},
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s1",
            "cwd": "/proj/vec",
            "timestamp": "2026-05-01T00:01:00Z",
            "message": {"role": "user", "content": "never use psycopg2 here, it is banned"},
        },
    ]
    import json as _json

    sess.write_text("\n".join(_json.dumps(x) for x in lines) + "\n")

    db = tmp_path / "index.db"
    monkeypatch.setenv("TOTAL_RECALL_VEC", "1")
    monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "none")

    rc = main(
        [
            "--db",
            str(db),
            "rebuild",
            "--yes",
            "--projects-root",
            str(projects),
        ]
    )
    assert rc == 0, "rebuild should exit 0"

    # The vec backfill should have created + populated chunk_embeddings.
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "chunk_embeddings" in tables, "vec schema not applied by rebuild"
        n = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
        assert n > 0, "rebuild did not backfill any embeddings"
    finally:
        conn.close()
