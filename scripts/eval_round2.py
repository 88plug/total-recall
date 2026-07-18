#!/usr/bin/env python3
"""Round-2 brand-new product evals (post model-card crank learnings).

Does NOT reuse scripts/eval_product_models.py CORPUS. Fresh domains, shapes,
and failure modes drawn from the 2.3.4 card + eval lap:

  * domain instruct > web instruct
  * kind re-rank (decision > domain_fact near-miss)
  * dense_primary hybrid
  * qwen3.5:2b wants schema + few-shot, not greedy
  * FTS owns symbols; dense owns paraphrase
  * project_key cwd pooling

Suites:
  R1  session-realistic long notes (multi-sentence extractions)
  R2  zero-lexical-overlap paraphrases
  R3  confusable tech twins (both present; query must pick the winner)
  R4  operator corrections / bans voice
  R5  exact symbols (error codes, tags, hosts) — hybrid must not lose FTS
  R6  kind stress (decision vs near-miss domain_fact)
  R7  reject/negative memory ("what did we ban/drop")
  R8  cwd isolation (wrong project must not leak into top-1)
  R9  instruct A/B on this new corpus
  L1  brand-new LLM JSON tasks (card sampling + schema)
  L2  production refine (machines + vocab) on new entities

Usage:
  cd total-recall
  .venv/bin/python scripts/eval_round2.py
  .venv/bin/python scripts/eval_round2.py --out docs/eval-round2.md
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
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# =============================================================================
# Brand-new corpora (no strings shared with eval_product_models CORPUS)
# =============================================================================

# R1 — session-note style (longer, messy, realistic)
SESSION_PAIRS: list[tuple[str, str]] = [
    (
        "why did the GPU box thrash last night",
        "session 2026-07-16: ollama keep_alive was 5m so qwen3-embedding unloaded mid-backfill; "
        "VRAM thrash fixed by TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 and pinning the product daemon",
    ),
    (
        "how do we avoid double-billing the cluster",
        "standing rule: always set a hard max_tokens and stream-cancel on client disconnect; "
        "gpustack jobs without a budget were the July outage root cause",
    ),
    (
        "where is the operator psyche profile loaded from",
        "CLAUDE.md points at ~/.claude/andrew-mello-psyche.md; SessionStart injects the "
        "shorthand be first / be trusted / be right — right is a hard constraint",
    ),
    (
        "what broke the marketplace tip last push",
        "rebase conflict on marketplace.json version field; resolved by taking the higher "
        "calver stamp after sources/total-recall fast-forward",
    ),
    (
        "how do we keep subagents honest about project rules",
        "SubagentStart hook inject-claudemd-into-subagents.sh re-injects global + project "
        "CLAUDE.md into Explore/Plan which otherwise skip them",
    ),
    (
        "preferred way to drive the real Firefox session",
        "use screen-mcp OS screenshots + synthetic clicks; never chrome-devtools — user is "
        "logged in only in Firefox and CDP opens an unauthenticated Chrome",
    ),
    (
        "how should rebuild behave when ollama is missing",
        "product-owned path: ensure_product_ollama downloads bin under plugin data, serves, "
        "pulls embed+chat; system ollama is fallback only",
    ),
    (
        "what is the standing ban on dangerous rm",
        "never propose rm -rf \"$VAR\"/* or unbraced rm -rf $VAR; empty VAR expands to rm -rf /*; "
        "use fresh mktemp -d per probe instead of wipe-and-reuse",
    ),
]

# R2 — zero/near-zero lexical overlap (synonym only)
PARAPHRASE_PAIRS: list[tuple[str, str]] = [
    (
        "how do we pin inference weights in VRAM",
        "TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 stops the embed model from unloading between batches",
    ),
    (
        "tool for local metasearch without leaving the LAN",
        "searxng MCP prefers 192.168.1.211:8890 then docker then tailscale backends",
    ),
    (
        "policy for inventing hostnames the user never named",
        "refine_machines must drop hallucinated keys not present in the input candidate set",
    ),
    (
        "when to spawn a fresh reviewer instead of self-check",
        "independent review: do not review your own work in the same context; spawn a fresh agent",
    ),
    (
        "default fusion order for keyword plus vector recall",
        "dense_primary keeps vector rank order and only appends FTS hits the dense leg missed",
    ),
    (
        "how query text is wrapped for the 0.6b embedder",
        "Instruct line describes session memory retrieval then Query: plus the user string; documents stay raw",
    ),
]

# R3 — confusable twins: both docs in index; query must pick the decided one
TWIN_PAIRS: list[tuple[str, str]] = [
    (
        "which local search backend is canonical",
        "decision: searxng-plus on LAN is the primary search MCP; duckduckgo-only scrapers are banned",
    ),
    (
        "which screen automation path is allowed for auth'd UI",
        "decision: screen-mcp only for the operator's real desktop; playwright is disposable unauthenticated browsers only",
    ),
    (
        "which embed stack is product default",
        "decision: ollama qwen3-embedding:0.6b is the only dense path; fastembed and ONNX were removed in format v2",
    ),
    (
        "which chat model refines extractions",
        "decision: qwen3.5:2b for refine; larger 9b lost to think-leak null definitions on CPU bake-off",
    ),
]
TWIN_DISTRACTORS: list[str] = [
    "we briefly prototyped duckduckgo HTML scrape before searxng",
    "playwright was evaluated for dashboard checks then rejected for auth pages",
    "fastembed gte-modernbert was the pre-v2 default and must not return",
    "qwen3.5:9b was measured and produced all-null definitions under think leak",
    "chrome-devtools MCP exists but is banned for authenticated operator sessions",
    "nomic-embed-text remains a manual override only, not product default",
]

# R4 — corrections / bans voice
CORRECTION_PAIRS: list[tuple[str, str]] = [
    (
        "stop suggesting cloud only logging",
        "correction: ship logs to self-hosted loki; never default to a vendor SaaS log sink",
    ),
    (
        "do not use force push on main",
        "ban: force-push to main is forbidden; use revert commits or a new PR",
    ),
    (
        "never put secrets in repo env samples",
        "ban: .env.example must contain placeholders only; real secrets live in vault",
    ),
    (
        "quit inventing MCP servers that are not connected",
        "correction: enumerate live MCP tools each session; do not assume servers from a static list",
    ),
]

# R5 — exact symbols (FTS should help; hybrid must not bury)
SYMBOL_PAIRS: list[tuple[str, str]] = [
    (
        "TOTAL_RECALL_EMBED_KEEP_ALIVE",
        "env TOTAL_RECALL_EMBED_KEEP_ALIVE defaults to -1 to pin the embed model",
    ),
    (
        "qwen3-embedding:0.6b",
        "product embed model tag is qwen3-embedding:0.6b (1024-d MRL, Q8_0 ~639MB)",
    ),
    (
        "inject-claudemd-into-subagents.sh",
        "hook path ~/.claude/hooks/inject-claudemd-into-subagents.sh fires on SubagentStart",
    ),
    (
        "NullPointerException in auth middleware",
        "incident note: NullPointerException in auth middleware after cookie SameSite change",
    ),
    (
        "web-01",
        "ops: nginx restarted on web-01 after certificate rotation",
    ),
]

# R6 — kind stress: decision target vs near-miss domain_fact
KIND_STRESS: list[tuple[str, str]] = [
    (
        "what is the standing rule for embed unload",
        "decision: pin embed with keep_alive=-1 for the whole backfill window",
    ),
    (
        "canonical dense model choice",
        "decision: ship qwen3-embedding:0.6b; upgrade to 4b only if instruction-heavy eval fails",
    ),
    (
        "how hybrid ranks when FTS is noisy",
        "decision: dense_primary fusion is default so weak FTS cannot steal top-1",
    ),
]
KIND_NEAR_MISS: list[str] = [
    "we experimented with keep_alive=5m and saw thrash; that is not the standing rule",
    "qwen3-embedding:4b scores higher on MTEB but is not the product default size",
    "equal-weight RRF was the old hybrid mode and regressed paraphrase P@1 to 0.40",
    "some operators still set keep_alive=30m for interactive chat only",
]

# R7 — reject / negative memory
REJECT_PAIRS: list[tuple[str, str]] = [
    (
        "what packaging approach did we drop",
        "rejected: publishing total-recall as a pure pip wheel without the plugin marketplace path",
    ),
    (
        "which browser automation did we ban for logged-in work",
        "rejected: chrome-devtools for any flow that needs the operator's real Firefox cookies",
    ),
    (
        "what embed path is retired",
        "rejected: in-process ONNX/fastembed embeds; format v2 is ollama-only",
    ),
]

# R8 — cwd isolation
CWD_A = "/proj/alpha-memory"
CWD_B = "/proj/beta-unrelated"
CWD_PAIRS_A: list[tuple[str, str]] = [
    (
        "deploy target for this service",
        "alpha deploys to the farmgpu harness region us-east via argo",
    ),
    (
        "database for alpha",
        "alpha uses cockroach for multi-region state",
    ),
]
CWD_NOISE_B: list[str] = [
    "beta deploys only to a laptop kind cluster",
    "beta uses sqlite for everything including prod",
    "beta never touches farmgpu",
]

# Soft distractors (office noise)
SOFT: list[str] = [
    "plant watering rota is on the fridge",
    "the 3d printer filament order ships tuesday",
    "someone left a half-eaten sandwich in the lab fridge",
    "the HVAC filter was replaced in march",
    "board game night is every other thursday",
]


# =============================================================================
# LLM tasks (new)
# =============================================================================

LLM_TASKS: list[dict] = [
    {
        "name": "extract_standing_ban",
        "system": (
            "Extract a standing ban from the user message.\n"
            'JSON only: {"banned": string, "scope": string}\n'
            "Copy exact artifact names. Example: "
            '\'Never force-push main\' -> {"banned":"force-push main","scope":"git"}'
        ),
        "user": "Standing ban: never run rm -rf on an unbraced shell variable. Use mktemp -d.",
        "schema": {
            "type": "object",
            "properties": {
                "banned": {"type": "string"},
                "scope": {"type": "string"},
            },
            "required": ["banned", "scope"],
        },
        "require_keys": ["banned", "scope"],
        "must_contain_any": ["rm", "variable", "mktemp", "unbraced", "brace"],
    },
    {
        "name": "extract_preference_voice",
        "system": (
            "Extract operator communication preference.\n"
            'JSON: {"preference": string, "evidence": string}\n'
            "evidence must be a short span from the user text."
        ),
        "user": "Talk like me: lowercase, terse, no preambles, we-framing, zero emoji.",
        "schema": {
            "type": "object",
            "properties": {
                "preference": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["preference", "evidence"],
        },
        "require_keys": ["preference", "evidence"],
        "must_contain_any": ["terse", "lowercase", "emoji", "preamble", "we"],
    },
    {
        "name": "classify_not_correction",
        "system": (
            "Is this a correction of the assistant?\n"
            'JSON: {"is_correction": boolean, "summary": string}'
        ),
        "user": "Looks good — ship it after the tests pass.",
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
        "bool_expect": False,
    },
    {
        "name": "multi_host_evidence",
        "system": (
            "List machines that appear as hosts. Include evidence substring from text.\n"
            'JSON: {"hosts":[{"name":string,"evidence":string}]}\n'
            "Empty list if none. Never invent."
        ),
        "user": "Pulled metrics from gpu-box-3 and restarted caddy on edge-relay after the cert swap.",
        "schema": {
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                        "required": ["name", "evidence"],
                    },
                }
            },
            "required": ["hosts"],
        },
        "require_keys": ["hosts"],
        "must_contain_any": ["gpu-box-3", "edge-relay", "gpu", "relay"],
    },
    {
        "name": "grounded_null_def",
        "system": (
            "Define the term ONLY from the snippet. Do not invent meaning.\n"
            "If the snippet only repeats the term or is too thin, definition MUST be null.\n"
            'JSON: {"term":string,"definition":string|null}\n'
            "Examples:\n"
            '  term=sharechain snippet="sharechain links p2pool shares" -> '
            '{"term":"sharechain","definition":"links p2pool shares"}\n'
            '  term=blorptree snippet="see blorptree" -> '
            '{"term":"blorptree","definition":null}\n'
            '  term=xyzzy snippet="xyzzy" -> {"term":"xyzzy","definition":null}'
        ),
        "user": 'term: "blorptree"\nsnippet: "see blorptree"\nReturn definition null if insufficient.',
        "schema": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "definition": {"type": ["string", "null"]},
            },
            "required": ["term", "definition"],
        },
        "require_keys": ["term", "definition"],
        "expect_null_key": "definition",
    },
    {
        "name": "decision_with_reject",
        "system": (
            "Extract chosen tool and rejected alternative.\n"
            'JSON: {"chosen":string,"rejected":string|null}'
        ),
        "user": "We standardized on screen-mcp for desktop drive; playwright stays for headless public pages only.",
        "schema": {
            "type": "object",
            "properties": {
                "chosen": {"type": "string"},
                "rejected": {"type": ["string", "null"]},
            },
            "required": ["chosen", "rejected"],
        },
        "require_keys": ["chosen", "rejected"],
        "must_contain_any": ["screen", "playwright", "mcp"],
    },
]


# =============================================================================
# Metrics helpers
# =============================================================================


@dataclass
class RankMetrics:
    p_at_1: float = 0.0
    p_at_5: float = 0.0
    mrr: float = 0.0
    n: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def add(self, ranks: list[str], target: str, latency_ms: float) -> None:
        self.n += 1
        self.latencies_ms.append(latency_ms)
        if target in ranks[:1]:
            self.p_at_1 += 1
        else:
            self.misses.append(target[:70])
        if target in ranks[:5]:
            self.p_at_5 += 1
        try:
            self.mrr += 1.0 / (ranks.index(target) + 1)
        except ValueError:
            pass

    def finalize(self) -> dict:
        n = max(self.n, 1)
        lats = sorted(self.latencies_ms) or [0.0]

        def pct(p: float) -> float:
            i = min(len(lats) - 1, max(0, int(round(p * (len(lats) - 1)))))
            return lats[i]

        return {
            "n": self.n,
            "p@1": self.p_at_1 / n,
            "p@5": self.p_at_5 / n,
            "mrr": self.mrr / n,
            "miss_rate@1": 1.0 - (self.p_at_1 / n),
            "latency_ms_p50": pct(0.5),
            "miss_samples": self.misses[:8],
        }


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-12)


def _stamp(conn, kind: str, content: str, cwd: str, ts: int, uuid: str, score: float) -> None:
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


def _contents(hits) -> list[str]:
    out = []
    for h in hits:
        c = getattr(h, "content", None)
        if c is None and isinstance(h, dict):
            c = h.get("content")
        if c is not None:
            out.append(c)
    return out


def _build_db(embedder, rows: list[tuple[str, str, str]], cwd: str = "/proj/r2"):
    """rows: (kind, content, uuid_prefix)"""
    from index.db import connect
    from vec.store import apply_vec_schema, backfill_all

    tmp = Path(tempfile.mkdtemp()) / "r2.db"
    conn = connect(tmp)
    ts = 1_720_000_000
    for i, (kind, content, _pfx) in enumerate(rows):
        _stamp(conn, kind, content, cwd, ts + i, f"r2-{i}", 0.7 if kind != "domain_fact" else 0.45)
    conn.commit()
    apply_vec_schema(
        conn,
        dim=embedder.dim(),
        model=embedder.model or "qwen3-embedding:0.6b",
        backend=embedder.backend or "ollama",
    )
    t0 = time.perf_counter()
    rep = backfill_all(conn, embedder=embedder)
    return conn, rep, time.perf_counter() - t0


def _run_suite(
    conn,
    embedder,
    pairs: list[tuple[str, str]],
    cwd: str,
    label: str,
) -> dict:
    from vec.rrf import hybrid_search
    from vec.store import vec_search

    pure, fts, hyb = RankMetrics(), RankMetrics(), RankMetrics()
    for query, target in pairs:
        t0 = time.perf_counter()
        pure.add(_contents(vec_search(conn, query, embedder=embedder, limit=10, cwd=cwd)),
                 target, (time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        fts.add(_contents(hybrid_search(conn, query, embedder=None, limit=10, cwd=cwd)),
                target, (time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        hyb.add(_contents(hybrid_search(conn, query, embedder=embedder, limit=10, cwd=cwd)),
                target, (time.perf_counter() - t0) * 1000)
    return {
        "label": label,
        "n": len(pairs),
        "pure_dense": pure.finalize(),
        "fts_only": fts.finalize(),
        "hybrid": hyb.finalize(),
    }


# =============================================================================
# Suite runners
# =============================================================================


def eval_retrieval_round2() -> dict:
    from vec.embed import Embedder, QWEN3_QUERY_INSTRUCT_MEMORY, QWEN3_QUERY_INSTRUCT_WEB
    from vec.runtime import ensure_product_ollama

    status = ensure_product_ollama(embed=True, chat=False, pull=True)
    os.environ.pop("TOTAL_RECALL_EMBED_INSTRUCT", None)
    embedder = Embedder()
    _ = embedder.dim()
    cwd = "/proj/r2"

    suites: dict[str, dict] = {}

    # --- R1 session ---
    rows = [("decision", t, "s") for _, t in SESSION_PAIRS]
    rows += [("domain_fact", d, "soft") for d in SOFT]
    conn, rep, secs = _build_db(embedder, rows, cwd)
    try:
        suites["R1_session"] = _run_suite(conn, embedder, SESSION_PAIRS, cwd, "R1_session")
        suites["R1_session"]["backfill_s"] = round(secs, 3)
        suites["R1_session"]["embedded"] = rep.extractions_embedded
    finally:
        conn.close()

    # --- R2 paraphrase ---
    rows = [("decision", t, "p") for _, t in PARAPHRASE_PAIRS]
    rows += [("domain_fact", d, "soft") for d in SOFT]
    conn, _, _ = _build_db(embedder, rows, cwd)
    try:
        suites["R2_paraphrase"] = _run_suite(conn, embedder, PARAPHRASE_PAIRS, cwd, "R2_paraphrase")
    finally:
        conn.close()

    # --- R3 twins ---
    rows = [("decision", t, "tw") for _, t in TWIN_PAIRS]
    rows += [("domain_fact", d, "td") for d in TWIN_DISTRACTORS]
    rows += [("domain_fact", d, "soft") for d in SOFT]
    conn, _, _ = _build_db(embedder, rows, cwd)
    try:
        suites["R3_twins"] = _run_suite(conn, embedder, TWIN_PAIRS, cwd, "R3_twins")
    finally:
        conn.close()

    # --- R4 corrections ---
    rows = []
    for _, t in CORRECTION_PAIRS:
        kind = "ban" if t.startswith("ban:") else "correction"
        rows.append((kind, t, "c"))
    rows += [("domain_fact", d, "soft") for d in SOFT]
    conn, _, _ = _build_db(embedder, rows, cwd)
    try:
        suites["R4_corrections"] = _run_suite(conn, embedder, CORRECTION_PAIRS, cwd, "R4_corrections")
    finally:
        conn.close()

    # --- R5 symbols ---
    rows = [("domain_fact", t, "sym") for _, t in SYMBOL_PAIRS]
    rows += [("domain_fact", d, "soft") for d in SOFT]
    # confusers with partial token overlap
    rows += [
        ("domain_fact", "unrelated keep_alive discussion for chat models only", "x"),
        ("domain_fact", "some other embedding tag embeddinggemma:300m exists", "x"),
        ("domain_fact", "web-02 was decommissioned last year", "x"),
    ]
    conn, _, _ = _build_db(embedder, rows, cwd)
    try:
        suites["R5_symbols"] = _run_suite(conn, embedder, SYMBOL_PAIRS, cwd, "R5_symbols")
    finally:
        conn.close()

    # --- R6 kind stress ---
    rows = [("decision", t, "k") for _, t in KIND_STRESS]
    rows += [("domain_fact", d, "km") for d in KIND_NEAR_MISS]
    conn, _, _ = _build_db(embedder, rows, cwd)
    try:
        suites["R6_kind"] = _run_suite(conn, embedder, KIND_STRESS, cwd, "R6_kind")
    finally:
        conn.close()

    # --- R7 reject ---
    rows = [("decision", t, "rj") for _, t in REJECT_PAIRS]
    rows += [("domain_fact", d, "soft") for d in SOFT]
    rows += [
        ("domain_fact", "pip wheels are still fine for some internal tools", "x"),
        ("domain_fact", "chrome is installed for unrelated browsing", "x"),
        ("domain_fact", "ONNX runtime is used by an unrelated speech project", "x"),
    ]
    conn, _, _ = _build_db(embedder, rows, cwd)
    try:
        suites["R7_reject"] = _run_suite(conn, embedder, REJECT_PAIRS, cwd, "R7_reject")
    finally:
        conn.close()

    # --- R8 cwd isolation ---
    from index.db import connect
    from vec.store import apply_vec_schema, backfill_all, vec_search

    tmp = Path(tempfile.mkdtemp()) / "r2-cwd.db"
    conn = connect(tmp)
    ts = 1_720_100_000
    for i, (_, content) in enumerate(CWD_PAIRS_A):
        _stamp(conn, "decision", content, CWD_A, ts + i, f"a{i}", 0.8)
    for j, content in enumerate(CWD_NOISE_B):
        _stamp(conn, "decision", content, CWD_B, ts + 50 + j, f"b{j}", 0.8)
    conn.commit()
    apply_vec_schema(
        conn, dim=embedder.dim(), model=embedder.model or "qwen3-embedding:0.6b",
        backend=embedder.backend or "ollama",
    )
    backfill_all(conn, embedder=embedder)
    cwd_ok = 0
    cwd_n = 0
    leaks: list[str] = []
    try:
        for query, target in CWD_PAIRS_A:
            hits = vec_search(conn, query, embedder=embedder, limit=5, cwd=CWD_A)
            ranks = _contents(hits)
            cwd_n += 1
            # top-1 must be alpha target; no beta content in top-1
            if ranks and ranks[0] == target and not any("beta" in r for r in ranks[:1]):
                cwd_ok += 1
            else:
                leaks.append(f"q={query[:40]} top={ranks[0][:50] if ranks else None}")
        suites["R8_cwd"] = {
            "label": "R8_cwd",
            "n": cwd_n,
            "isolation_rate": cwd_ok / max(cwd_n, 1),
            "leaks": leaks,
            "hybrid": {"p@1": cwd_ok / max(cwd_n, 1), "p@5": cwd_ok / max(cwd_n, 1), "mrr": cwd_ok / max(cwd_n, 1), "n": cwd_n, "miss_rate@1": 1 - cwd_ok / max(cwd_n, 1), "latency_ms_p50": 0, "miss_samples": leaks},
            "pure_dense": {"p@1": cwd_ok / max(cwd_n, 1)},
            "fts_only": {"p@1": None},
        }
    finally:
        conn.close()

    # --- R9 instruct A/B on session+paraphrase queries (in-memory) ---
    def ab_score(prefix: str, pairs: list[tuple[str, str]]) -> dict:
        emb = Embedder()
        emb._load()
        emb._query_prefix = prefix
        docs = [t for _, t in pairs] + SOFT + TWIN_DISTRACTORS[:3]
        dvecs = emb.embed(docs, as_query=False)
        m = RankMetrics()
        for q, target in pairs:
            qv = emb.embed([q], as_query=True)[0]
            scored = sorted(((_cos(qv, dv), docs[i]) for i, dv in enumerate(dvecs)), reverse=True)
            m.add([c for _, c in scored[:10]], target, 0.0)
        return m.finalize()

    ab_pairs = SESSION_PAIRS + PARAPHRASE_PAIRS
    web = ab_score(QWEN3_QUERY_INSTRUCT_WEB, ab_pairs)
    mem = ab_score(QWEN3_QUERY_INSTRUCT_MEMORY, ab_pairs)
    suites["R9_instruct_ab"] = {
        "web": web,
        "memory": mem,
        "memory_p@1_delta": round(mem["p@1"] - web["p@1"], 4),
        "memory_mrr_delta": round(mem["mrr"] - web["mrr"], 4),
        "memory_wins_or_ties": mem["p@1"] + 1e-9 >= web["p@1"],
    }

    # Gates
    def hp(name: str) -> dict:
        return suites[name]["hybrid"]

    gates = {
        "R1_session_p@1_ge_0.75": hp("R1_session")["p@1"] >= 0.75,
        "R1_session_p@5_ge_0.9": hp("R1_session")["p@5"] >= 0.9,
        "R2_paraphrase_p@1_ge_0.66": hp("R2_paraphrase")["p@1"] >= 0.66,
        "R2_paraphrase_p@5_ge_0.85": hp("R2_paraphrase")["p@5"] >= 0.85,
        "R3_twins_p@1_ge_0.75": hp("R3_twins")["p@1"] >= 0.75,
        "R4_corrections_p@1_ge_0.75": hp("R4_corrections")["p@1"] >= 0.75,
        "R5_symbols_p@1_ge_0.8": hp("R5_symbols")["p@1"] >= 0.8,
        "R5_hybrid_ge_fts_p@1": hp("R5_symbols")["p@1"] + 0.05 >= suites["R5_symbols"]["fts_only"]["p@1"],
        "R6_kind_p@1_ge_0.66": hp("R6_kind")["p@1"] >= 0.66,
        "R7_reject_p@1_ge_0.66": hp("R7_reject")["p@1"] >= 0.66,
        "R8_cwd_isolation_ge_1.0": suites["R8_cwd"]["isolation_rate"] >= 1.0,
        "R9_memory_instruct_not_worse": suites["R9_instruct_ab"]["memory_wins_or_ties"],
        # Macro: mean hybrid P@1 across R1–R7
        "macro_hybrid_p@1_ge_0.75": (
            statistics.fmean(hp(k)["p@1"] for k in (
                "R1_session", "R2_paraphrase", "R3_twins", "R4_corrections",
                "R5_symbols", "R6_kind", "R7_reject",
            )) >= 0.75
        ),
    }

    macro = {
        "mean_hybrid_p@1": statistics.fmean(
            hp(k)["p@1"] for k in (
                "R1_session", "R2_paraphrase", "R3_twins", "R4_corrections",
                "R5_symbols", "R6_kind", "R7_reject",
            )
        ),
        "mean_hybrid_p@5": statistics.fmean(
            hp(k)["p@5"] for k in (
                "R1_session", "R2_paraphrase", "R3_twins", "R4_corrections",
                "R5_symbols", "R6_kind", "R7_reject",
            )
        ),
        "mean_miss_rate@1": statistics.fmean(
            hp(k)["miss_rate@1"] for k in (
                "R1_session", "R2_paraphrase", "R3_twins", "R4_corrections",
                "R5_symbols", "R6_kind", "R7_reject",
            )
        ),
    }

    return {
        "runtime": status,
        "model": embedder.model,
        "dim": embedder.dim(),
        "suites": suites,
        "macro": macro,
        "gates": gates,
    }


def eval_llm_round2() -> dict:
    from extractors.llm.client import LLMClient
    from extractors.llm.refine_machines import refine_machines
    from extractors.llm.refine_ontology import refine_vocabulary_definitions
    from vec.runtime import ensure_product_ollama

    status = ensure_product_ollama(embed=False, chat=True, pull=True)
    client = LLMClient(provider="auto", model="qwen3.5:2b")
    if not client.available:
        return {"error": "qwen3.5:2b unavailable", "gates": {"llm_available": False}}

    results = []
    ok = 0
    for task in LLM_TASKS:
        t0 = time.perf_counter()
        out = client.generate_json(
            system=task["system"],
            user=task["user"],
            schema=task.get("schema"),
            temperature=None,
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
                reasons.append(f"missing_any:{any_needles}")
            bk = task.get("bool_key")
            if bk is not None and bool(out.get(bk)) is not bool(task.get("bool_expect")):
                passed = False
                reasons.append(f"bool_mismatch:{bk}")
            nk = task.get("expect_null_key")
            if nk is not None and out.get(nk) is not None:
                passed = False
                reasons.append(f"expected_null:{nk} got={out.get(nk)!r}")
        if passed:
            ok += 1
        results.append({"name": task["name"], "pass": passed, "latency_ms": round(ms, 1),
                        "output": out, "reasons": reasons})

    # Production refine — new entities
    machines = {
        "gpu-box-3": {"role": "gpu", "ip": "10.9.0.3", "tailscale": True, "hits": 8},
        "edge-relay": {"role": "edge", "ip": "10.9.0.9", "tailscale": True, "hits": 4},
        "Thursday": {"role": None, "ip": None, "tailscale": False, "hits": 2},
        "playwright": {"role": None, "ip": None, "tailscale": False, "hits": 3},
    }
    contexts = {
        "gpu-box-3": ["ssh gpu-box-3 pulled nvidia-smi"],
        "edge-relay": ["caddy on edge-relay reloaded certs"],
        "Thursday": ["board game night is every other Thursday"],
        "playwright": ["playwright is for headless public pages only"],
    }
    t0 = time.perf_counter()
    refined = refine_machines(machines, client=client, sample_contexts=contexts)
    machines_ms = (time.perf_counter() - t0) * 1000
    kept = set(refined.keys())
    machines_ok = "gpu-box-3" in kept and "edge-relay" in kept
    precision = machines_ok and "Thursday" not in kept and "playwright" not in kept

    terms = [
        {
            "term": "dense_primary",
            "frequency": 6,
            "category": "retrieval",
            "context_snippet": (
                "dense_primary hybrid keeps vector rank order first and only appends "
                "FTS hits the dense leg missed so weak keywords cannot steal top-1"
            ),
        },
        {
            "term": "product ollama",
            "frequency": 5,
            "category": "runtime",
            "context_snippet": (
                "product-owned ollama lives under the plugin data bin directory; "
                "total-recall starts serve and pulls embed plus chat models"
            ),
        },
    ]
    t0 = time.perf_counter()
    vocab_out = refine_vocabulary_definitions(terms, client=client)
    vocab_ms = (time.perf_counter() - t0) * 1000
    defs = {t["term"]: t.get("definition") for t in vocab_out}
    vocab_ok = bool(defs.get("dense_primary")) and isinstance(defs.get("dense_primary"), str) and len(defs["dense_primary"]) > 12

    return {
        "runtime": status,
        "model": client.model,
        "n": len(LLM_TASKS),
        "pass_rate": ok / len(LLM_TASKS),
        "tasks": results,
        "production_refine": {
            "machines_ms": round(machines_ms, 1),
            "kept": sorted(kept),
            "machines_ok": machines_ok,
            "precision": precision,
            "vocab_ms": round(vocab_ms, 1),
            "definitions": defs,
            "vocab_ok": vocab_ok,
        },
        "gates": {
            "llm_available": True,
            "json_pass_rate_ge_0.83": (ok / len(LLM_TASKS)) >= 0.83,
            "machines_hosts_kept": machines_ok,
            "machines_precision": precision,
            "vocab_ok": vocab_ok,
            "mean_latency_ms_lt_20000": statistics.fmean(r["latency_ms"] for r in results) < 20000,
        },
    }


def render_md(report: dict) -> str:
    lines = [
        "# total-recall eval round 2 (brand-new suites)",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        "Post model-card crank learnings. New corpora — not the 2.3.3/2.3.4 easy set.",
        "",
        "## Macro retrieval",
        "```json",
        json.dumps(report["retrieval"].get("macro"), indent=2),
        "```",
        "",
        "## Suites",
        "```json",
        json.dumps(report["retrieval"].get("suites"), indent=2, default=str),
        "```",
        "",
        "## LLM",
        "```json",
        json.dumps({k: v for k, v in report["llm"].items() if k != "runtime"}, indent=2, default=str),
        "```",
        "",
        "## Gates",
    ]
    all_g: dict[str, bool] = {}
    for section in ("retrieval", "llm"):
        for k, v in (report.get(section) or {}).get("gates", {}).items():
            all_g[f"{section}.{k}"] = bool(v)
    for k, v in all_g.items():
        lines.append(f"- `{'PASS' if v else 'FAIL'}` {k}")
    lines.append("")
    lines.append(
        f"**Overall: {'PASS' if all_g and all(all_g.values()) else 'FAIL'}** "
        f"({sum(all_g.values())}/{len(all_g)} gates)"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/eval-round2.md"))
    args = ap.parse_args()

    print("=== round2 retrieval ===", flush=True)
    retrieval = eval_retrieval_round2()
    print(json.dumps({
        "macro": retrieval["macro"],
        "gates": retrieval["gates"],
        "instruct_ab": retrieval["suites"].get("R9_instruct_ab"),
        **{k: v.get("hybrid") if isinstance(v, dict) and "hybrid" in v else v
           for k, v in retrieval["suites"].items() if k != "R9_instruct_ab"},
    }, indent=2, default=str), flush=True)
    for name, suite in retrieval["suites"].items():
        if name == "R9_instruct_ab":
            continue
        h = suite.get("hybrid") or {}
        print(
            f"  {name}: hybrid p@1={h.get('p@1')} p@5={h.get('p@5')} "
            f"miss@1={h.get('miss_rate@1')} misses={h.get('miss_samples')}",
            flush=True,
        )

    print("=== round2 llm ===", flush=True)
    llm = eval_llm_round2()
    print(json.dumps({k: llm[k] for k in llm if k not in ("runtime", "tasks")}, indent=2, default=str), flush=True)
    for t in llm.get("tasks") or []:
        print(f"  [{'PASS' if t['pass'] else 'FAIL'}] {t['name']} {t['latency_ms']}ms {t.get('reasons')}", flush=True)

    report = {"retrieval": retrieval, "llm": llm}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_md(report), encoding="utf-8")
    print(f"\nWrote {args.out}", flush=True)

    gates = []
    for section in ("retrieval", "llm"):
        gates.extend((report[section].get("gates") or {}).values())
    return 0 if gates and all(gates) else 1


if __name__ == "__main__":
    sys.exit(main())
