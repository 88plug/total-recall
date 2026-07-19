#!/usr/bin/env python3
"""Real eval: product ollama embeds + chat on live daemon.

Measures accuracy and latency of the product path (qwen3-embedding:0.6b dense,
hybrid FTS+dense_primary, qwen3.5:2b JSON refine). Includes:
  * easy paraphrase set
  * hard near-miss / zero-overlap set (card-crank stress)
  * instruct A/B (generic web vs domain memory)
  * card-aligned LLM sampling (no forced greedy on Qwen)

Usage:
  cd total-recall
  .venv/bin/python scripts/eval_product_models.py
  .venv/bin/python scripts/eval_product_models.py --out /tmp/tr-eval.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ---------------------------------------------------------------------------
# Easy paraphrase set (keyword-poor queries → semantic targets)
# ---------------------------------------------------------------------------

CORPUS: list[tuple[str, str]] = [
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
    ("where do we put feature flags", "launchdarkly owns all runtime feature toggles"),
    ("how to run unit tests", "pytest is the only supported test runner"),
    ("preferred shell on servers", "all ops scripts assume bash, not zsh or fish"),
    ("image registry for deploys", "push containers to ghcr not docker hub"),
    ("how we handle secrets rotation", "vault agent injects rotated creds every hour"),
]

# Hard: near-miss distractors share vocabulary; targets need semantics / exactness.
# Also zero-lexical-overlap paraphrases and confusable tech twins.
HARD_CORPUS: list[tuple[str, str]] = [
    (
        "what broke login after the oauth refactor",
        "session notes: OAuth callback state mismatch after SameSite cookie change; fixed by aligning redirect cookie flags",
    ),
    (
        "which postgres client library did we lock",
        "decision: standardize on asyncpg for all database access, not psycopg2",
    ),
    (
        "do not use the old python formatter",
        "correction: run ruff for linting and formatting, drop black — never reintroduce black",
    ),
    (
        "where do rotated credentials come from at runtime",
        "vault agent injects rotated creds every hour into the pod",
    ),
    (
        "how background work is scheduled without threads",
        "use a task queue with celery workers, not threads or asyncio fire-and-forget",
    ),
    (
        "prod cluster scheduler not docker compose",
        "we run everything on kubernetes in production; local docker-compose is dev only",
    ),
    (
        "event bus not rabbit",
        "services talk over nats jetstream, not rabbitmq or kafka",
    ),
    (
        "package manager that replaced pip",
        "use uv for installs and lockfiles, not pip or poetry",
    ),
    (
        "gitops path that applies manifests",
        "argocd syncs manifests to the cluster on merge to main",
    ),
    (
        "who owns runtime toggles",
        "launchdarkly owns all runtime feature toggles; no ad-hoc env flags",
    ),
    (
        "where exceptions go in production",
        "exceptions are reported to sentry in prod; do not email stack traces",
    ),
    (
        "container push destination",
        "push containers to ghcr not docker hub",
    ),
    (
        "operator meaning of harness in this repo",
        "in our setup harness means the Claude Code / Grok plugin runner, not livestock or test harnesses",
    ),
    (
        "how project keys pool worktree memory",
        "project_key collapses git worktree cwds back to the owning repository root for memory pooling",
    ),
    (
        "dense embed model we ship",
        "product dense embeds use qwen3-embedding 0.6b via managed ollama, not fastembed",
    ),
]

# Near-miss rows: share tokens with HARD targets but are wrong answers.
HARD_NEAR_MISS: list[str] = [
    "we evaluated psycopg2 for legacy scripts but did not standardize on it",
    "black is still allowed in one abandoned experiment branch",
    "rabbitmq was the previous bus before the nats migration",
    "docker hub was used historically before ghcr",
    "poetry was considered then rejected in favor of uv",
    "env files are only for local throwaway demos, not secrets management policy",
    "kafka is used by the analytics team, not our product services",
    "kubernetes local kind clusters are not production",
    "pytest plugins for coverage are optional; runner is still pytest",
    "sentry is disabled in local dev to cut noise",
    "OAuth login UI copy was redesigned last quarter unrelated to cookie flags",
    "harness also means a horse collar in the style guide joke channel",
    "worktrees are fine; we just map them via project_key",
    "fastembed was removed; do not re-enable ONNX embeds",
    "celery beat schedules periodic tasks; not a substitute for the worker pool decision",
]

DISTRACTORS: list[str] = [
    "the standup is at 10am daily",
    "the office wifi password rotates monthly",
    "lunch is catered on fridays",
    "the logo uses the brand teal #1aa",
    "remember to expense the conference tickets",
    "the dog is allowed in the office on fridays",
    "parking validation is at the front desk",
    "holiday party is the second week of december",
    "the coffee machine needs descaling weekly",
    "team offsite is in austin this year",
]

# LLM JSON micro-tasks — card-aligned: schema + few-shot, family sampling (not temp=0)
LLM_TASKS: list[dict] = [
    {
        "name": "extract_decision",
        "system": (
            "Extract a software decision from the user sentence.\n"
            "JSON only. Copy library/tool names exactly.\n"
            'Schema: {"decision": string, "topic": string}\n'
            "Example: 'We picked redis for cache' -> "
            '{"decision":"redis for cache","topic":"caching"}'
        ),
        "user": "We decided to use asyncpg for all postgres access going forward.",
        "schema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string"},
                "topic": {"type": "string"},
            },
            "required": ["decision", "topic"],
        },
        "require_keys": ["decision", "topic"],
        "must_contain_any": ["asyncpg", "postgres", "database"],
        "temperature": None,  # Qwen non-thinking card defaults
    },
    {
        "name": "extract_ban",
        "system": (
            "Extract a ban/forbidden practice.\n"
            'JSON only: {"banned": string, "reason": string}\n'
            "Example: 'Never force-push main' -> "
            '{"banned":"force-push main","reason":"protects shared history"}'
        ),
        "user": "Never commit .env files with secrets. Always use vault.",
        "schema": {
            "type": "object",
            "properties": {
                "banned": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["banned", "reason"],
        },
        "require_keys": ["banned", "reason"],
        "must_contain_any": ["env", "secret", "vault"],
        "temperature": None,
    },
    {
        "name": "classify_correction",
        "system": (
            "Does the user correct the assistant?\n"
            'JSON only: {"is_correction": boolean, "summary": string}\n'
            "Examples:\n"
            '  "No, use ruff not black" -> {"is_correction": true, "summary":"prefer ruff over black"}\n'
            '  "Thanks, that works" -> {"is_correction": false, "summary":"acceptance"}'
        ),
        "user": "No, use ruff not black for formatting.",
        "schema": {
            "type": "object",
            "properties": {
                "is_correction": {"type": "boolean"},
                "summary": {"type": "string"},
            },
            "required": ["is_correction", "summary"],
        },
        "require_keys": ["is_correction", "summary"],
        "bool_key": "is_correction",
        "bool_expect": True,
        "temperature": None,
    },
    {
        "name": "machine_ner",
        "system": (
            "Extract hostnames and services explicitly named as hosts/services.\n"
            "Do not invent. Prefer empty lists over guesses.\n"
            'JSON: {"hosts": [string], "services": [string]}\n'
            'Example: "ssh web-01" -> {"hosts":["web-01"],"services":[]}'
        ),
        "user": "Restarted nginx on web-01 and redis on cache-02 after the deploy.",
        "schema": {
            "type": "object",
            "properties": {
                "hosts": {"type": "array", "items": {"type": "string"}},
                "services": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["hosts", "services"],
        },
        "require_keys": ["hosts", "services"],
        "must_contain_any": ["web-01", "web", "nginx", "cache"],
        "temperature": None,
    },
    {
        "name": "vocab_def",
        "system": (
            "Extract the defined term and definition from the sentence only.\n"
            "Do not use world knowledge. If unclear return null definition.\n"
            'JSON: {"term": string, "definition": string|null}\n'
            "Example: \"'sharechain' means linked p2pool shares\" -> "
            '{"term":"sharechain","definition":"linked p2pool shares"}'
        ),
        "user": "In our setup, 'harness' means the Claude Code / Grok plugin runner, not livestock.",
        "schema": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "definition": {"type": ["string", "null"]},
            },
            "required": ["term", "definition"],
        },
        "require_keys": ["term", "definition"],
        "must_contain_any": ["harness", "plugin", "claude", "grok"],
        "temperature": None,
    },
    {
        "name": "null_when_missing",
        "system": (
            "Extract hostname if any. If none, hosts must be empty.\n"
            'JSON: {"hosts": [string]}\n'
            'Example: "restarted the wifi" -> {"hosts":[]}'
        ),
        "user": "The logo uses brand teal and lunch is catered on Fridays.",
        "schema": {
            "type": "object",
            "properties": {"hosts": {"type": "array", "items": {"type": "string"}}},
            "required": ["hosts"],
        },
        "require_keys": ["hosts"],
        "expect_empty_list_key": "hosts",
        "temperature": None,
    },
]


@dataclass
class RankMetrics:
    p_at_1: float = 0.0
    p_at_5: float = 0.0
    mrr: float = 0.0
    n: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def add(self, ranks: list[str], target: str, latency_ms: float) -> None:
        self.n += 1
        self.latencies_ms.append(latency_ms)
        if target in ranks[:1]:
            self.p_at_1 += 1
        if target in ranks[:5]:
            self.p_at_5 += 1
        try:
            r = ranks.index(target) + 1
            self.mrr += 1.0 / r
        except ValueError:
            pass

    def finalize(self) -> dict:
        n = max(self.n, 1)
        lats = sorted(self.latencies_ms) or [0.0]

        def pct(p: float) -> float:
            if not lats:
                return 0.0
            i = min(len(lats) - 1, max(0, int(round(p * (len(lats) - 1)))))
            return lats[i]

        return {
            "n": self.n,
            "p@1": self.p_at_1 / n,
            "p@5": self.p_at_5 / n,
            "mrr": self.mrr / n,
            "latency_ms_p50": pct(0.5),
            "latency_ms_p95": pct(0.95),
            "latency_ms_mean": statistics.fmean(lats) if lats else 0.0,
        }


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-12)


def _stamp_row(conn, kind: str, content: str, cwd: str, ts: int, uuid: str, score: float) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(extractions)").fetchall()}
    if "project_key" in cols:
        conn.execute(
            "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
            "score, scope, project_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, content, "s", cwd, ts, uuid, score, "project", cwd),
        )
    else:
        conn.execute(
            "INSERT INTO extractions(kind, content, session_id, cwd, ts, source_uuid, "
            "score, scope) VALUES (?,?,?,?,?,?,?,?)",
            (kind, content, "s", cwd, ts, uuid, score, "project"),
        )


def _hit_contents(hits) -> list[str]:
    out: list[str] = []
    for h in hits:
        c = getattr(h, "content", None)
        if c is None and isinstance(h, dict):
            c = h.get("content")
        if c is not None:
            out.append(c)
    return out


def _run_retrieval_suite(
    conn,
    embedder,
    pairs: list[tuple[str, str]],
    cwd: str,
    *,
    label: str,
) -> dict:
    pure = RankMetrics()
    fts = RankMetrics()
    hyb = RankMetrics()
    pairwise_ok = 0
    pairwise_n = 0
    misses_at_1: list[str] = []

    from vec.rrf import hybrid_search
    from vec.store import vec_search

    for query, target in pairs:
        t0 = time.perf_counter()
        vhits = vec_search(conn, query, embedder=embedder, limit=10, cwd=cwd)
        pure.add(_hit_contents(vhits), target, (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        fhits = hybrid_search(conn, query, embedder=None, limit=10, cwd=cwd)
        fts.add(_hit_contents(fhits), target, (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        hhits = hybrid_search(conn, query, embedder=embedder, limit=10, cwd=cwd)
        hyb_ranks = _hit_contents(hhits)
        hyb.add(hyb_ranks, target, (time.perf_counter() - t0) * 1000)
        if target not in hyb_ranks[:1]:
            misses_at_1.append(query[:60])

        qv = embedder.embed([query], as_query=True)[0]
        tv = embedder.embed([target], as_query=False)[0]
        dv = embedder.embed([DISTRACTORS[0]], as_query=False)[0]
        pairwise_n += 1
        if _cos(qv, tv) > _cos(qv, dv):
            pairwise_ok += 1

    pf, ff, hf = pure.finalize(), fts.finalize(), hyb.finalize()
    return {
        "label": label,
        "n": len(pairs),
        "pure_dense": pf,
        "fts_only": ff,
        "hybrid": hf,
        "pairwise_target_beats_distractor": pairwise_ok / max(pairwise_n, 1),
        "hybrid_miss_at_1": misses_at_1,
        "hybrid_miss_rate_at_1": len(misses_at_1) / max(len(pairs), 1),
    }


def _pairwise_instruct_ab(easy_pairs: list[tuple[str, str]]) -> dict:
    """Compare generic web instruct vs product memory instruct (query-only).

    Documents are raw either way — no re-index needed. Measures pure cosine
    ranking quality on the easy set under each query prefix.
    """
    from vec.embed import QWEN3_QUERY_INSTRUCT_MEMORY, QWEN3_QUERY_INSTRUCT_WEB, Embedder

    def score_with(prefix: str) -> dict:
        emb = Embedder()
        emb._load()
        emb._query_prefix = prefix
        pure = RankMetrics()
        # In-memory rank over full easy targets + distractors
        docs = [t for _, t in easy_pairs] + DISTRACTORS
        dvecs = emb.embed(docs, as_query=False)
        for query, target in easy_pairs:
            t0 = time.perf_counter()
            qv = emb.embed([query], as_query=True)[0]
            scored = sorted(
                ((_cos(qv, dv), docs[i]) for i, dv in enumerate(dvecs)),
                key=lambda x: -x[0],
            )
            ranks = [c for _, c in scored[:10]]
            pure.add(ranks, target, (time.perf_counter() - t0) * 1000)
        return pure.finalize()

    web = score_with(QWEN3_QUERY_INSTRUCT_WEB)
    mem = score_with(QWEN3_QUERY_INSTRUCT_MEMORY)
    return {
        "web_instruct": web,
        "memory_instruct": mem,
        "memory_p@1_delta": round(mem["p@1"] - web["p@1"], 4),
        "memory_mrr_delta": round(mem["mrr"] - web["mrr"], 4),
        "memory_wins_or_ties_p@1": mem["p@1"] + 1e-9 >= web["p@1"],
    }


def _build_eval_db(
    embedder,
    cwd: str,
    targets: list[tuple[str, str]],
    *,
    near_miss: list[str] | None = None,
    soft: list[str] | None = None,
    target_kind: str = "decision",
):
    """Fresh DB + backfill. Easy and hard suites use separate indexes."""
    from index.db import connect
    from vec.store import apply_vec_schema, backfill_all

    tmp = Path(tempfile.mkdtemp()) / "eval.db"
    conn = connect(tmp)
    ts = 1_700_000_000
    for i, (_q, content) in enumerate(targets):
        _stamp_row(conn, target_kind, content, cwd, ts + i, f"t{i}", 0.75)
    j = 0
    for d in near_miss or []:
        _stamp_row(conn, "domain_fact", d, cwd, ts + 400 + j, f"n{j}", 0.45)
        j += 1
    for d in soft or []:
        _stamp_row(conn, "domain_fact", d, cwd, ts + 800 + j, f"s{j}", 0.4)
        j += 1
    conn.commit()
    apply_vec_schema(
        conn,
        dim=embedder.dim(),
        model=embedder.model or "qwen3-embedding:0.6b",
        backend=embedder.backend or "ollama",
    )
    t0 = time.perf_counter()
    report = backfill_all(conn, embedder=embedder)
    backfill_s = time.perf_counter() - t0
    return conn, report, backfill_s


def eval_embeds() -> dict:
    from vec.embed import Embedder, _qwen3_query_instruct
    from vec.runtime import ensure_product_ollama

    status = ensure_product_ollama(embed=True, chat=False, pull=True)
    # Ensure product memory instruct (not leftover env from prior A/B)
    os.environ.pop("TOTAL_RECALL_EMBED_INSTRUCT", None)
    embedder = Embedder()
    _ = embedder.dim()
    instruct = _qwen3_query_instruct()

    cwd = "/proj/eval"
    # Easy: comparable to prior 2.3.3 baseline (no hard-target clones)
    easy_conn, easy_rep, easy_bf = _build_eval_db(
        embedder,
        cwd,
        CORPUS,
        soft=DISTRACTORS,
    )
    try:
        easy = _run_retrieval_suite(easy_conn, embedder, CORPUS, cwd, label="easy")
    finally:
        easy_conn.close()

    # Hard: targets + near-miss domain_facts that share vocabulary
    hard_conn, hard_rep, hard_bf = _build_eval_db(
        embedder,
        cwd,
        HARD_CORPUS,
        near_miss=HARD_NEAR_MISS,
        soft=DISTRACTORS,
    )
    try:
        hard = _run_retrieval_suite(hard_conn, embedder, HARD_CORPUS, cwd, label="hard")
    finally:
        hard_conn.close()

    instruct_ab = _pairwise_instruct_ab(CORPUS)

    easy_h, hard_h = easy["hybrid"], hard["hybrid"]
    easy_p, hard_p = easy["pure_dense"], hard["pure_dense"]

    return {
        "runtime": status,
        "model": embedder.model,
        "backend": embedder.backend,
        "dim": embedder.dim(),
        "query_instruct": instruct[:120],
        "backfill": {
            "easy_embedded": easy_rep.extractions_embedded,
            "easy_seconds": round(easy_bf, 3),
            "hard_embedded": hard_rep.extractions_embedded,
            "hard_seconds": round(hard_bf, 3),
        },
        "easy": easy,
        "hard": hard,
        "instruct_ab": instruct_ab,
        "pure_dense": easy_p,
        "fts_only": easy["fts_only"],
        "hybrid": easy_h,
        "pairwise_target_beats_distractor": easy["pairwise_target_beats_distractor"],
        "gates": {
            "hybrid_not_worse_than_fts_p@5": easy_h["p@5"] >= easy["fts_only"]["p@5"],
            "hybrid_p@1_near_dense": easy_h["p@1"] + 0.05 >= easy_p["p@1"],
            "easy_hybrid_p@1_ge_0.75": easy_h["p@1"] >= 0.75,
            "easy_pure_dense_p@1_ge_0.7": easy_p["p@1"] >= 0.7,
            "easy_pure_dense_mrr_ge_0.75": easy_p["mrr"] >= 0.75,
            "easy_pairwise_ge_0.9": easy["pairwise_target_beats_distractor"] >= 0.9,
            "hard_hybrid_p@1_ge_0.6": hard_h["p@1"] >= 0.6,
            "hard_hybrid_p@5_ge_0.85": hard_h["p@5"] >= 0.85,
            "hard_pure_dense_p@1_ge_0.55": hard_p["p@1"] >= 0.55,
            "hard_miss_rate_at_1_le_0.4": hard["hybrid_miss_rate_at_1"] <= 0.4,
            "memory_instruct_not_worse_than_web": instruct_ab["memory_wins_or_ties_p@1"],
        },
    }


def eval_llm() -> dict:
    from extractors.llm.client import LLMClient
    from vec.runtime import ensure_product_ollama

    status = ensure_product_ollama(embed=False, chat=True, pull=True)
    client = LLMClient(provider="auto", model="qwen3.5:2b")
    if not client.available:
        return {"error": "qwen3.5:2b not available", "runtime": status}

    results = []
    ok = 0
    for task in LLM_TASKS:
        t0 = time.perf_counter()
        # Card: Qwen non-thinking uses temp≈0.7 + schema — not greedy temp=0.
        temp = task.get("temperature", None)
        out = client.generate_json(
            system=task["system"],
            user=task["user"],
            schema=task.get("schema"),
            temperature=temp,
        )
        ms = (time.perf_counter() - t0) * 1000
        passed = True
        reasons: list[str] = []
        if out is None:
            passed = False
            reasons.append("null_response")
        else:
            for k in task["require_keys"]:
                if k not in out:
                    passed = False
                    reasons.append(f"missing_key:{k}")
            blob = json.dumps(out).lower()
            any_needles = task.get("must_contain_any") or []
            if any_needles and not any(n.lower() in blob for n in any_needles):
                passed = False
                reasons.append(f"missing_any_of:{any_needles}")
            for needle in task.get("must_contain") or []:
                if needle.lower() not in blob:
                    passed = False
                    reasons.append(f"missing_text:{needle}")
            bk = task.get("bool_key")
            if bk is not None and bool(out.get(bk)) is not bool(task.get("bool_expect")):
                passed = False
                reasons.append(f"bool_mismatch:{bk}")
            ek = task.get("expect_empty_list_key")
            if ek is not None:
                val = out.get(ek)
                if not isinstance(val, list) or len(val) != 0:
                    passed = False
                    reasons.append(f"expected_empty_list:{ek}")
        if passed:
            ok += 1
        results.append(
            {
                "name": task["name"],
                "pass": passed,
                "latency_ms": round(ms, 1),
                "output": out,
                "reasons": reasons,
            }
        )

    prod = eval_production_refine(client)
    # Harder production gate: drop day-name + library false positives
    machines = prod.get("machines") or {}
    precision_ok = (
        prod.get("machines_ok", False)
        and machines.get("dropped_monday", False)
        and machines.get("dropped_asyncpg", False)
    )

    return {
        "runtime": status,
        "model": client.model,
        "n": len(LLM_TASKS),
        "pass_rate": ok / len(LLM_TASKS),
        "tasks": results,
        "production_refine": prod,
        "gates": {
            "json_task_pass_rate_ge_0.8": (ok / len(LLM_TASKS)) >= 0.8,
            "mean_latency_ms_lt_15000": statistics.fmean(r["latency_ms"] for r in results) < 15000,
            "machines_refine_keeps_real_hosts": prod.get("machines_ok", False),
            "machines_refine_precision": precision_ok,
            "vocab_refine_defines_term": prod.get("vocab_ok", False),
        },
    }


def eval_production_refine(client) -> dict:
    """Exercise real refine_* entrypoints used on rebuild cold path."""
    from extractors.llm.refine_machines import refine_machines
    from extractors.llm.refine_ontology import refine_vocabulary_definitions

    machines = {
        "web-01": {"role": "web", "ip": "10.0.0.1", "tailscale": False, "hits": 12},
        "cache-02": {"role": "cache", "ip": "10.0.0.2", "tailscale": True, "hits": 5},
        "Monday": {
            "role": None,
            "ip": None,
            "tailscale": False,
            "hits": 2,
        },  # false positive day-name
        "asyncpg": {"role": None, "ip": None, "tailscale": False, "hits": 3},  # library not host
    }
    contexts = {
        "web-01": ["sshd on web-01 restarted after deploy"],
        "cache-02": ["redis on cache-02 OOM, scaled memory"],
        "Monday": ["see you Monday for the standup"],
        "asyncpg": ["switched the driver to asyncpg"],
    }
    t0 = time.perf_counter()
    refined = refine_machines(machines, client=client, sample_contexts=contexts)
    machines_ms = (time.perf_counter() - t0) * 1000

    kept = set(refined.keys())
    # Must keep real hosts; ideally drop day-name / library (soft: keep hosts is hard gate)
    machines_ok = "web-01" in kept and "cache-02" in kept
    machines_precision = {
        "kept": sorted(kept),
        "kept_real_hosts": machines_ok,
        "dropped_monday": "Monday" not in kept,
        "dropped_asyncpg": "asyncpg" not in kept,
    }

    terms = [
        {
            "term": "harness",
            "frequency": 9,
            "category": "tooling",
            "context_snippet": (
                "the harness is the Claude Code / Grok plugin runner that loads "
                "MCP servers and skills for the session"
            ),
        },
        {
            "term": "project_key",
            "frequency": 4,
            "category": "code",
            "context_snippet": (
                "project_key collapses git worktree cwds back to the owning "
                "repository root for memory pooling"
            ),
        },
    ]
    t0 = time.perf_counter()
    vocab_out = refine_vocabulary_definitions(terms, client=client)
    vocab_ms = (time.perf_counter() - t0) * 1000
    defs = {t["term"]: t.get("definition") for t in vocab_out}
    vocab_ok = (
        bool(defs.get("harness"))
        and isinstance(defs.get("harness"), str)
        and len(defs["harness"]) > 10
    )

    return {
        "machines_ms": round(machines_ms, 1),
        "machines": machines_precision,
        "machines_ok": machines_ok,
        "vocab_ms": round(vocab_ms, 1),
        "vocab_definitions": defs,
        "vocab_ok": vocab_ok,
    }


def eval_product_runtime() -> dict:
    from vec.runtime import (
        daemon_reachable,
        ensure_product_ollama,
        list_model_names,
        model_present,
        resolve_ollama_bin,
    )

    st = ensure_product_ollama(embed=True, chat=True, pull=True)
    names = list_model_names() or []
    return {
        "daemon_reachable": daemon_reachable(),
        "bin": str(resolve_ollama_bin()) if resolve_ollama_bin() else None,
        "ensure": st,
        "models_present": {
            "qwen3-embedding:0.6b": model_present("qwen3-embedding:0.6b", names),
            "qwen3.5:2b": model_present("qwen3.5:2b", names),
        },
        "gates": {
            "daemon_up": daemon_reachable(),
            "embed_model": model_present("qwen3-embedding:0.6b", names),
            "chat_model": model_present("qwen3.5:2b", names),
        },
    }


def eval_mtp_tensors() -> dict:
    """Confirm default chat model ships MTP heads (not embed)."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/show",
            data=json.dumps({"name": "qwen3.5:2b"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "gates": {"has_mtp_tensors": False}}
    tensors = data.get("tensors") or []
    mtp = [t for t in tensors if isinstance(t, dict) and str(t.get("name", "")).startswith("mtp.")]
    return {
        "model": "qwen3.5:2b",
        "n_tensors": len(tensors) if isinstance(tensors, list) else 0,
        "n_mtp_tensors": len(mtp),
        "sample_mtp": [t.get("name") for t in mtp[:5]],
        "gates": {"has_mtp_tensors": len(mtp) >= 1},
    }


def render_md(report: dict) -> str:
    lines = [
        "# total-recall product model eval",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        "## Product runtime",
        "```json",
        json.dumps(report["runtime"], indent=2, default=str),
        "```",
        "",
        "## MTP (qwen3.5:2b)",
        "```json",
        json.dumps(report["mtp"], indent=2, default=str),
        "```",
        "",
        "## Dense embeds (qwen3-embedding:0.6b)",
        f"Query instruct (truncated): `{(report.get('embeds') or {}).get('query_instruct', '')}`",
        "",
        "### Easy / hard / instruct A/B",
        "```json",
        json.dumps(
            {
                "easy": (report.get("embeds") or {}).get("easy"),
                "hard": (report.get("embeds") or {}).get("hard"),
                "instruct_ab": (report.get("embeds") or {}).get("instruct_ab"),
                "backfill": (report.get("embeds") or {}).get("backfill"),
                "model": (report.get("embeds") or {}).get("model"),
                "dim": (report.get("embeds") or {}).get("dim"),
            },
            indent=2,
            default=str,
        ),
        "```",
        "",
        "## LLM refine (qwen3.5:2b)",
        "```json",
        json.dumps(
            {k: v for k, v in (report.get("llm") or {}).items() if k not in ("runtime",)},
            indent=2,
            default=str,
        ),
        "```",
        "",
        "## Gate summary",
    ]
    all_gates: dict[str, bool] = {}
    for section in ("runtime", "mtp", "embeds", "llm"):
        g = (report.get(section) or {}).get("gates") or {}
        for k, v in g.items():
            all_gates[f"{section}.{k}"] = bool(v)
    for k, v in all_gates.items():
        lines.append(f"- `{'PASS' if v else 'FAIL'}` {k}")
    lines.append("")
    lines.append(
        f"**Overall: {'PASS' if all(all_gates.values()) else 'FAIL'}** "
        f"({sum(all_gates.values())}/{len(all_gates)} gates)"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/eval-product-models.md"))
    args = ap.parse_args()

    print("=== product runtime ===", flush=True)
    runtime = eval_product_runtime()
    print(json.dumps(runtime, indent=2, default=str), flush=True)

    print("=== MTP tensors ===", flush=True)
    mtp = eval_mtp_tensors()
    print(json.dumps(mtp, indent=2, default=str), flush=True)

    print("=== embeds ===", flush=True)
    embeds = eval_embeds()
    print(
        json.dumps({k: embeds[k] for k in embeds if k != "runtime"}, indent=2, default=str),
        flush=True,
    )

    print("=== llm ===", flush=True)
    llm = eval_llm()
    print(
        json.dumps(
            {k: llm[k] for k in llm if k not in ("runtime", "tasks")}, indent=2, default=str
        ),
        flush=True,
    )
    for t in llm.get("tasks") or []:
        print(
            f"  [{'PASS' if t['pass'] else 'FAIL'}] {t['name']} {t['latency_ms']}ms {t.get('reasons')}",
            flush=True,
        )

    report = {"runtime": runtime, "mtp": mtp, "embeds": embeds, "llm": llm}
    md = render_md(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md, encoding="utf-8")
    print(f"\nWrote {args.out}", flush=True)

    gates = []
    for section in ("runtime", "mtp", "embeds", "llm"):
        gates.extend((report[section].get("gates") or {}).values())
    return 0 if gates and all(gates) else 1


if __name__ == "__main__":
    sys.exit(main())
