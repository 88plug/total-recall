#!/usr/bin/env python3
"""Real eval: product ollama embeds + chat on live daemon.

Measures accuracy and latency of the v2.3.2 path (qwen3-embedding:0.6b dense,
hybrid FTS+RRF, qwen3.5:2b JSON refine). Writes a markdown report.

Usage:
  cd total-recall
  .venv/bin/python scripts/eval_product_models.py
  .venv/bin/python scripts/eval_product_models.py --out /tmp/tr-eval.md
"""

from __future__ import annotations

import argparse
import json
import math
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
# Labeled paraphrase set (stresses keyword retrievers; rewards semantics)
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

DISTRACTORS: list[str] = [
    "the standup is at 10am daily",
    "the office wifi password rotates monthly",
    "lunch is catered on fridays",
    "the logo uses the brand teal #1aa",
    "remember to expense the conference tickets",
    "the dog is allowed in the office on fridays",
    "parking validation is at the front desk",
    "holiday party is the second week of december",
]

# LLM JSON micro-tasks (product qwen3.5:2b) — clear extraction prompts
LLM_TASKS: list[dict] = [
    {
        "name": "extract_decision",
        "system": (
            "You extract software engineering decisions from a user sentence. "
            'Reply with JSON only: {"decision": string (what was chosen), '
            '"topic": string (short topic)}. '
            "Copy technical identifiers (library/tool names) into decision."
        ),
        "user": "We decided to use asyncpg for all postgres access going forward.",
        "require_keys": ["decision", "topic"],
        "must_contain_any": ["asyncpg", "postgres", "database"],
    },
    {
        "name": "extract_ban",
        "system": (
            "You extract a ban/forbidden practice from a user sentence. "
            'Reply with JSON only: {"banned": string (what is forbidden), '
            '"reason": string}. Quote the forbidden artifact if named.'
        ),
        "user": "Never commit .env files with secrets. Always use vault.",
        "require_keys": ["banned", "reason"],
        "must_contain_any": ["env", "secret", "vault"],
    },
    {
        "name": "classify_correction",
        "system": (
            "Does this user message correct the assistant? "
            'Reply with JSON only: {"is_correction": boolean, "summary": string}.'
        ),
        "user": "No, use ruff not black for formatting.",
        "require_keys": ["is_correction", "summary"],
        "bool_key": "is_correction",
        "bool_expect": True,
    },
    {
        "name": "machine_ner",
        "system": (
            "Extract hostnames and services. "
            'Reply with JSON only: {"hosts": [string], "services": [string]}.'
        ),
        "user": "Restarted nginx on web-01 and redis on cache-02 after the deploy.",
        "require_keys": ["hosts", "services"],
        "must_contain_any": ["web-01", "web", "nginx", "cache"],
    },
    {
        "name": "vocab_def",
        "system": (
            "Extract the defined term and its definition. "
            'Reply with JSON only: {"term": string, "definition": string}.'
        ),
        "user": "In our setup, 'harness' means the Claude Code / Grok plugin runner, not livestock.",
        "require_keys": ["term", "definition"],
        "must_contain_any": ["harness", "plugin", "claude", "grok"],
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


def eval_embeds() -> dict:
    from index.db import connect
    from vec.embed import Embedder
    from vec.rrf import hybrid_search
    from vec.runtime import ensure_product_ollama
    from vec.store import apply_vec_schema, backfill_all, vec_search

    status = ensure_product_ollama(embed=True, chat=False, pull=True)
    embedder = Embedder()
    # force load
    _ = embedder.dim()

    cwd = "/proj/eval"
    tmp = Path(tempfile.mkdtemp()) / "eval.db"
    conn = connect(tmp)
    try:
        ts = 1_700_000_000
        for i, (_q, content) in enumerate(CORPUS):
            _stamp_row(conn, "decision", content, cwd, ts + i, f"t{i}", 0.7)
        for j, d in enumerate(DISTRACTORS):
            _stamp_row(conn, "domain_fact", d, cwd, ts + 100 + j, f"d{j}", 0.5)
        conn.commit()

        apply_vec_schema(
            conn, dim=embedder.dim(), model=embedder.model or "qwen3-embedding:0.6b",
            backend=embedder.backend or "ollama",
        )
        t0 = time.perf_counter()
        report = backfill_all(conn, embedder=embedder)
        backfill_s = time.perf_counter() - t0

        pure = RankMetrics()
        fts = RankMetrics()
        hyb = RankMetrics()
        pairwise_ok = 0
        pairwise_n = 0

        for query, target in CORPUS:
            # pure dense
            t0 = time.perf_counter()
            vhits = vec_search(conn, query, embedder=embedder, limit=10, cwd=cwd)
            pure.add([h.content for h in vhits], target, (time.perf_counter() - t0) * 1000)

            # FTS only
            t0 = time.perf_counter()
            fhits = hybrid_search(conn, query, embedder=None, limit=10, cwd=cwd)
            fts.add(
                [getattr(h, "content", None) or (h.get("content") if isinstance(h, dict) else None)
                 for h in fhits],
                target,
                (time.perf_counter() - t0) * 1000,
            )

            # hybrid
            t0 = time.perf_counter()
            hhits = hybrid_search(conn, query, embedder=embedder, limit=10, cwd=cwd)
            hyb.add(
                [getattr(h, "content", None) or (h.get("content") if isinstance(h, dict) else None)
                 for h in hhits],
                target,
                (time.perf_counter() - t0) * 1000,
            )

            # pairwise: target should outrank a random distractor under pure cosine
            qv = embedder.embed([query], as_query=True)[0]
            tv = embedder.embed([target], as_query=False)[0]
            dv = embedder.embed([DISTRACTORS[0]], as_query=False)[0]
            pairwise_n += 1
            if _cos(qv, tv) > _cos(qv, dv):
                pairwise_ok += 1

        return {
            "runtime": status,
            "model": embedder.model,
            "backend": embedder.backend,
            "dim": embedder.dim(),
            "backfill": {
                "embedded": report.extractions_embedded,
                "chunks": report.chunks_written,
                "seconds": round(backfill_s, 3),
            },
            "pure_dense": pure.finalize(),
            "fts_only": fts.finalize(),
            "hybrid": hyb.finalize(),
            "pairwise_target_beats_distractor": pairwise_ok / max(pairwise_n, 1),
            "gates": {
                "hybrid_not_worse_than_fts_p@5": hyb.finalize()["p@5"] >= fts.finalize()["p@5"],
                # dense_primary must not regress pure dense top-1 (was 0.40 vs 0.80)
                "hybrid_p@1_near_dense": hyb.finalize()["p@1"] + 0.05 >= pure.finalize()["p@1"],
                "hybrid_p@1_ge_0.75": hyb.finalize()["p@1"] >= 0.75,
                "pure_dense_p@1_ge_0.5": pure.finalize()["p@1"] >= 0.5,
                "pure_dense_mrr_ge_0.6": pure.finalize()["mrr"] >= 0.6,
                "pairwise_ge_0.9": (pairwise_ok / max(pairwise_n, 1)) >= 0.9,
            },
        }
    finally:
        conn.close()


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
        out = client.generate_json(
            system=task["system"],
            user=task["user"],
            schema=None,
            temperature=0.0,
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
            if bk is not None and out is not None:
                if bool(out.get(bk)) is not bool(task.get("bool_expect")):
                    passed = False
                    reasons.append(f"bool_mismatch:{bk}")
        if passed:
            ok += 1
        results.append({
            "name": task["name"],
            "pass": passed,
            "latency_ms": round(ms, 1),
            "output": out,
            "reasons": reasons,
        })

    # Production refine paths (machines filter + vocab definitions)
    prod = eval_production_refine(client)

    return {
        "runtime": status,
        "model": client.model,
        "n": len(LLM_TASKS),
        "pass_rate": ok / len(LLM_TASKS),
        "tasks": results,
        "production_refine": prod,
        "gates": {
            "json_task_pass_rate_ge_0.6": (ok / len(LLM_TASKS)) >= 0.6,
            "mean_latency_ms_lt_15000": statistics.fmean(r["latency_ms"] for r in results) < 15000,
            "machines_refine_keeps_real_hosts": prod.get("machines_ok", False),
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
        "Monday": {"role": None, "ip": None, "tailscale": False, "hits": 2},  # false positive day-name
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
    vocab_ok = bool(defs.get("harness")) and isinstance(defs.get("harness"), str) and len(defs["harness"]) > 10

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
        model_present,
        resolve_ollama_bin,
        list_model_names,
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
        "```json",
        json.dumps(report["embeds"], indent=2, default=str),
        "```",
        "",
        "## LLM refine (qwen3.5:2b)",
        "```json",
        json.dumps(report["llm"], indent=2, default=str),
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
    lines.append(f"**Overall: {'PASS' if all(all_gates.values()) else 'FAIL'}** "
                 f"({sum(all_gates.values())}/{len(all_gates)} gates)")
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
    print(json.dumps({k: embeds[k] for k in embeds if k != "runtime"}, indent=2, default=str), flush=True)

    print("=== llm ===", flush=True)
    llm = eval_llm()
    print(json.dumps({k: llm[k] for k in llm if k not in ("runtime", "tasks")}, indent=2, default=str), flush=True)
    for t in llm.get("tasks") or []:
        print(f"  [{'PASS' if t['pass'] else 'FAIL'}] {t['name']} {t['latency_ms']}ms {t.get('reasons')}", flush=True)

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
