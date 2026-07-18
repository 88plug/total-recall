#!/usr/bin/env python3
"""Adversarial 10× eval — large hard suite against product hybrid path.

Scale: ~400 labeled queries (10× prior HARD40), single polluted index with:
  * antonym / reject twins (true decision + opposite near-miss)
  * confusable tech twins (A vs B both present)
  * zero-overlap paraphrases
  * symbol / id exactness (hosts, env, model tags, errors)
  * kind stress (decision target vs domain_fact noise)
  * session-realistic long notes
  * soft office distractors

Reports pure dense / FTS / hybrid P@1 P@5 MRR per family + macro.
Writes docs/eval-adversarial-10x.md

Usage:
  .venv/bin/python scripts/eval_adversarial_10x.py
  .venv/bin/python scripts/eval_adversarial_10x.py --out docs/eval-adversarial-10x.md
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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# =============================================================================
# Seed knowledge: (id, target, antonym_near_miss, query_templates)
# Each seed expands to len(templates) labeled pairs → scale.
# =============================================================================

# 80 seeds × 5 queries = 400 pairs
_SEEDS: list[tuple[str, str, str, list[str]]] = [
    (
        "keep_alive",
        "decision: pin embed with TOTAL_RECALL_EMBED_KEEP_ALIVE=-1 for the whole backfill window",
        "near-miss: keep_alive=5m was tried and caused VRAM thrash; not the standing pin policy",
        [
            "how we keep embed weights resident in VRAM",
            "stop ollama unloading the embed model mid-backfill",
            "env that pins embed residency",
            "why did embedding thrash when keep_alive was short",
            "standing rule for embed model unload",
        ],
    ),
    (
        "truncate",
        "decision: ollama embed uses truncate=false so oversize chunks fail loud not silently",
        "near-miss: truncate=true is ollama default and hides oversize inputs",
        [
            "stop silent oversize chunk loss on embed",
            "should embed truncate long inputs",
            "fail loud when chunk exceeds context",
            "ollama embed truncate setting we ship",
            "how to avoid silent head truncation on index",
        ],
    ),
    (
        "asymmetric_instruct",
        "decision: only search queries get Instruct/Query prefix; documents embed raw",
        "near-miss: some stacks put instruct on documents too; we never do that",
        [
            "query side only gets the instruct wrapper",
            "do documents get qwen instruct prefix",
            "asymmetric encode for qwen3-embedding",
            "how query text is wrapped for the 0.6b embedder",
            "should we prefix indexed extraction text",
        ],
    ),
    (
        "dense_primary",
        "decision: hybrid default is dense_primary so weak FTS cannot steal paraphrase top-1",
        "near-miss: equal-weight RRF was legacy and regressed paraphrase P@1 to 0.40",
        [
            "default fusion that protects paraphrase top hits",
            "hybrid mode when FTS is noisy on synonyms",
            "why not equal weight RRF anymore",
            "default fusion order for keyword plus vector recall",
            "TOTAL_RECALL_HYBRID_MODE product default",
        ],
    ),
    (
        "exactish",
        "decision: exactish FTS promote when FTS top is phrase match and dense top is not",
        "near-miss: dense always wins is false for symbol queries like web-01",
        [
            "when FTS should still win top slot",
            "how hybrid handles web-01 vs web-02",
            "symbol query fusion rule",
            "model tag exact match over embedding near-miss",
            "FTS owns exact hostnames and env vars",
        ],
    ),
    (
        "kind_boost",
        "decision: dense re-rank boosts correction ban decision over domain_fact near-misses",
        "near-miss: domain_fact trivia used to outrank decisions without kind boost",
        [
            "kind that outranks trivia facts in dense",
            "why decisions beat domain_fact on close cosine",
            "kind-aware dense re-rank purpose",
            "stop trivia facts stealing decision top-1",
            "priority of bans versus domain facts",
        ],
    ),
    (
        "product_ollama",
        "decision: managed ollama binary lives under plugin data bin; system PATH is fallback only",
        "near-miss: system PATH ollama is not the primary product path",
        [
            "product binary location for ollama",
            "where does total-recall get ollama from",
            "BYO system ollama vs product owned",
            "how should rebuild behave when ollama is missing",
            "plugin data bin ollama serve",
        ],
    ),
    (
        "chat_2b",
        "decision: qwen3.5:2b is the refine model; 9b null-collapsed under think leak on CPU bake-off",
        "near-miss: qwen3.5:9b looked larger but lost define coverage to think leak",
        [
            "chat model for refine after bakeoff",
            "why not use 9b for JSON refine",
            "winning local LLM for vocabulary definitions",
            "DEFAULT_MODEL for llm client",
            "refine model that avoided all-null definitions",
        ],
    ),
    (
        "qwen_sampler",
        "decision: qwen non-thinking uses temperature 0.7 top_k 20 top_p 0.8 presence_penalty 1.5 seed 42",
        "near-miss: greedy temp=0 top_k=1 is gemma profile and hurts qwen instruction following",
        [
            "sampler family for qwen json refine",
            "should we force temperature zero on qwen",
            "official non-thinking sampling for qwen3.5",
            "reproducible qwen sampling with seed",
            "presence_penalty for refine calls",
        ],
    ),
    (
        "think_false",
        "decision: generate_json sets think false so qwen does not emit think blocks into JSON",
        "near-miss: leaving think on pollutes JSON with reasoning traces",
        [
            "thinking must be off for structured output",
            "why think false on ollama generate",
            "stop think tags in refine JSON",
            "payload field that disables reasoning trace",
            "think mode policy for extraction",
        ],
    ),
    (
        "machines_fewshot",
        "decision: refine_machines few-shot drops Monday and asyncpg while keeping web-01 and cache-02",
        "near-miss: day names and library tokens are not hostnames",
        [
            "how machines refine drops day names",
            "hostname classifier few-shot examples",
            "drop asyncpg from machines dict",
            "keep real hosts web-01 cache-02",
            "policy for inventing hostnames the user never named",
        ],
    ),
    (
        "null_def",
        "decision: vocab refine returns null definition when snippet is only the bare term",
        "near-miss: world-knowledge definitions without snippet evidence are invalid",
        [
            "null definition when snippet is empty signal",
            "when definition must be null",
            "anti-hallucination for term definitions",
            "grounded null for blorptree style terms",
            "snippet only rule for vocabulary",
        ],
    ),
    (
        "mtp",
        "decision: qwen3.5:2b ships mtp.* tensors for multi-token prediction on CUDA decode",
        "near-miss: MTP does not apply to the embed path or change correctness when verify works",
        [
            "MTP heads on chat weights",
            "does embed model use multi token prediction",
            "speculative decode tensors on qwen3.5 2b",
            "mtp speed only not quality",
            "confirm chat model has mtp tensors",
        ],
    ),
    (
        "num_ctx_embed",
        "decision: embed options set num_ctx 8192 not the full 32k window",
        "near-miss: num_ctx 2048 silently truncates longer chunks",
        [
            "num_ctx for embed requests",
            "context window we set on ollama embed",
            "avoid default 2048 on dense index",
            "embed num_ctx product default",
            "how large is embed context budget",
        ],
    ),
    (
        "format_v2",
        "decision: vec_meta format 2 marks ollama-only index; mismatch forces rebuild",
        "near-miss: format 1 fastembed indexes must rebuild to format 2",
        [
            "format version of dense index",
            "when dense index forces rebuild",
            "identity mismatch on model backend dim",
            "ollama-only embeds format contract",
            "vec_meta format field meaning",
        ],
    ),
    (
        "modernbert_ban",
        "decision: legacy HF embed ids like gte-modernbert are rejected; ollama-only path is mandatory",
        "near-miss: gte-modernbert was pre-v2 and must not return",
        [
            "why modernbert pin was removed",
            "legacy TOTAL_RECALL_EMBED_MODEL HF ids",
            "fastembed path status",
            "ONNX embeds retired",
            "what embed path is rejected",
        ],
    ),
    (
        "project_key",
        "decision: project_key maps worktree cwds back to owning repo root for pooled memory",
        "near-miss: raw cwd without project_key splits worktree memory incorrectly",
        [
            "project_key purpose for worktrees",
            "how worktree memory is pooled",
            "collapse git worktrees for recall",
            "why two worktrees share memory",
            "project_key vs raw cwd filter",
        ],
    ),
    (
        "subagent_hook",
        "decision: SubagentStart inject-claudemd-into-subagents.sh re-injects global and project CLAUDE.md",
        "near-miss: Explore and Plan skip CLAUDE.md without the SubagentStart hook",
        [
            "hook that injects CLAUDE.md into Explore",
            "how do we keep subagents honest about project rules",
            "SubagentStart inject claudemd",
            "why Explore ignores project CLAUDE.md by default",
            "hook path for subagent context injection",
        ],
    ),
    (
        "screen_mcp",
        "decision: screen-mcp is the only path for the operator real Firefox session; chrome-devtools is banned for auth UI",
        "near-miss: playwright is disposable unauthenticated browsers only; chrome-devtools opens unauth Chrome",
        [
            "only way to drive logged-in Firefox",
            "screen-mcp not chrome-devtools",
            "preferred way to drive the real Firefox session",
            "authenticated desktop automation path",
            "ban on chrome-devtools for operator login",
        ],
    ),
    (
        "rm_ban",
        "decision: never propose rm -rf on unbraced $VAR; empty expands to filesystem root; use fresh mktemp -d",
        "near-miss: rm -rf on a just-created mktemp dir is the only safe wipe pattern",
        [
            "standing ban on dangerous rm",
            "never rm -rf unbraced variable",
            "what is the standing ban on dangerous rm",
            "empty VAR expands to root with rm",
            "prefer mktemp over wipe-and-reuse",
        ],
    ),
    (
        "searxng",
        "decision: searxng MCP prefers LAN 192.168.1.211:8890 then docker then tailscale",
        "near-miss: duckduckgo-only scrapers are not the search MCP",
        [
            "searxng preferred first hop",
            "tool for local metasearch without leaving the LAN",
            "primary search MCP backend order",
            "192.168.1.211 searxng",
            "self-hosted metasearch chain",
        ],
    ),
    (
        "dim_1024",
        "decision: qwen3-embedding 0.6b native dim is 1024 with MRL down to 32",
        "near-miss: 4b embed is 2560-d not 1024",
        [
            "MRL native dimension of 0.6b",
            "embedding vector length we store",
            "dim of product dense model",
            "can we MRL truncate embed dims",
            "1024-d vectors from which model",
        ],
    ),
    (
        "last_token_pool",
        "decision: qwen3-embedding uses last-token pool not mean pool; L2 normalize before cosine",
        "near-miss: mean pooling would be wrong for this causal LM embedder",
        [
            "pooling mode of the embed model",
            "last token versus mean pool",
            "do we L2 normalize embeddings",
            "cosine after normalize equivalence",
            "EOS pooling for qwen3-embedding",
        ],
    ),
    (
        "upgrade_4b",
        "decision: stay on 0.6b embed; upgrade to 4b only if instruction-heavy multi-domain eval still fails after hybrid",
        "near-miss: jumping to 8b embed is rarely worth it after hybrid and rerank",
        [
            "when to upgrade embed size to 4b",
            "is 0.6b enough for total-recall",
            "should we ship 4b embedding by default",
            "0.6b versus 4b tradeoff",
            "pragmatic embed model size",
        ],
    ),
    (
        "num_ctx_llm",
        "decision: LLM refine client pins num_ctx 4096 for short refine jobs not 262k",
        "near-miss: num_ctx 0 negotiates model max and wastes KV cache on load",
        [
            "where chat refine num_ctx is capped",
            "KV cache size for JSON refine",
            "why not full 262k context on refine",
            "num_ctx 4096 meaning",
            "short context for extraction calls",
        ],
    ),
    (
        "json_retry",
        "decision: generate_json doubles num_predict once on JSONDecodeError from truncation",
        "near-miss: no retry leaves truncated arrays as hard failures",
        [
            "retry on truncated JSON",
            "num_predict double on parse fail",
            "unterminated string refine hardening",
            "truncation aware generate",
            "how we handle mid-JSON cutoffs",
        ],
    ),
    (
        "anti_echo",
        "decision: reject definitions that are near-verbatim copies of the snippet",
        "near-miss: echoing the snippet looks fluent but fails quality gates",
        [
            "anti-echo filter purpose",
            "stop copy-paste definitions",
            "jaccard filter on vocab refine",
            "definition must restate not quote",
            "echo rate failure mode",
        ],
    ),
    (
        "operator_voice",
        "decision: speak-like-operator skill matches lowercase terse we-framing zero emoji",
        "near-miss: emoji-heavy replies violate operator voice",
        [
            "operator voice skill name",
            "how should the assistant talk to the operator",
            "no emoji communication preference",
            "terse lowercase we framing",
            "speak-like-operator meaning",
        ],
    ),
    (
        "session_start",
        "decision: SessionStart emits operator context signpost for this cwd",
        "near-miss: Stop hook is reindex not the signpost",
        [
            "signpost hook event",
            "what fires at session start for memory",
            "operator briefing injection timing",
            "SessionStart total-recall payload",
            "cwd signpost purpose",
        ],
    ),
    (
        "user_prompt_submit",
        "decision: UserPromptSubmit runs decide_and_format for on-demand memory retrieval",
        "near-miss: PreCompact seeds continuity; different from UserPromptSubmit",
        [
            "retrieval hook event",
            "when are memories fetched on demand",
            "decide_and_format trigger",
            "UserPromptSubmit total-recall",
            "async memory inject on user turn",
        ],
    ),
    (
        "embed_model_tag",
        "decision: product dense model tag is qwen3-embedding:0.6b (Q8_0 ~639MB)",
        "near-miss: embeddinggemma is override-only not product default",
        [
            "default dense model tag",
            "qwen3-embedding:0.6b",
            "RECOMMENDED_OLLAMA_EMBED value",
            "which embed model do we ship",
            "0.6b ollama embedding package size",
        ],
    ),
    (
        "domain_instruct",
        "decision: query instruct is session-memory domain task not generic web search default",
        "near-miss: generic web instruct still works but is not product default",
        [
            "domain instruct beats web default",
            "TOTAL_RECALL_EMBED_INSTRUCT memory",
            "Instruct line for total-recall queries",
            "custom task description for embeds",
            "why not web search instruct string",
        ],
    ),
    (
        "lexical_rerank",
        "decision: hybrid re-ranks candidates by cosine plus token coverage after dense_primary merge",
        "near-miss: pure dense order alone lets near-misses without query tokens win",
        [
            "lexical re-rank in hybrid",
            "token coverage blend after fusion",
            "how hybrid recovers from dense near-miss",
            "coverage score in rrf merge",
            "post-merge candidate re-score",
        ],
    ),
    (
        "asyncpg",
        "decision: standardize on asyncpg for all postgres access not psycopg2",
        "near-miss: we evaluated psycopg2 for legacy scripts but did not standardize on it",
        [
            "which postgres client library did we lock",
            "postgres driver to use",
            "asyncpg versus psycopg2",
            "database driver standing decision",
            "what broke when people used psycopg2",
        ],
    ),
    (
        "ruff",
        "decision: run ruff for linting and formatting drop black never reintroduce black",
        "near-miss: black is still allowed in one abandoned experiment branch",
        [
            "do not use the old python formatter",
            "how to format python code",
            "ruff not black",
            "python lint format tool",
            "stop suggesting black",
        ],
    ),
    (
        "nats",
        "decision: services talk over nats jetstream not rabbitmq or kafka for product",
        "near-miss: rabbitmq was the previous bus before the nats migration",
        [
            "event bus not rabbit",
            "message bus technology",
            "nats versus rabbitmq",
            "product message bus choice",
            "jetstream standing decision",
        ],
    ),
    (
        "uv",
        "decision: use uv for installs and lockfiles not pip or poetry",
        "near-miss: poetry was considered then rejected in favor of uv",
        [
            "package manager that replaced pip",
            "python dependency manager",
            "uv not poetry",
            "how we lock python deps",
            "stop using pip freeze",
        ],
    ),
    (
        "k8s",
        "decision: we run everything on kubernetes in production; docker-compose is dev only",
        "near-miss: kubernetes local kind clusters are not production",
        [
            "prod cluster scheduler not docker compose",
            "container orchestration choice",
            "kubernetes production decision",
            "compose is not prod",
            "where production workloads schedule",
        ],
    ),
    (
        "vault",
        "decision: store credentials in vault never in env files; vault agent injects rotated creds",
        "near-miss: env files are only for local throwaway demos not secrets policy",
        [
            "secrets management approach",
            "where do rotated credentials come from at runtime",
            "never commit .env secrets",
            "vault agent injection",
            "secrets rotation standing rule",
        ],
    ),
    (
        "ghcr",
        "decision: push containers to ghcr not docker hub",
        "near-miss: docker hub was used historically before ghcr",
        [
            "container push destination",
            "image registry for deploys",
            "ghcr not docker hub",
            "where we push images",
            "registry standing decision",
        ],
    ),
    (
        "argocd",
        "decision: argocd syncs manifests to the cluster on merge to main",
        "near-miss: manual kubectl apply is not the gitops path",
        [
            "gitops path that applies manifests",
            "how do we deploy",
            "argocd sync policy",
            "manifests apply on merge",
            "deploy mechanism standing decision",
        ],
    ),
    (
        "sentry",
        "decision: exceptions are reported to sentry in prod; do not email stack traces",
        "near-miss: sentry is disabled in local dev to cut noise",
        [
            "where exceptions go in production",
            "error monitoring service",
            "sentry prod policy",
            "stop emailing stack traces",
            "exception reporting destination",
        ],
    ),
    (
        "pytest",
        "decision: pytest is the only supported test runner",
        "near-miss: pytest plugins for coverage are optional; runner is still pytest",
        [
            "how to run unit tests",
            "test runner standing decision",
            "only supported python test tool",
            "unittest versus pytest",
            "CI test command",
        ],
    ),
    (
        "launchdarkly",
        "decision: launchdarkly owns all runtime feature toggles; no ad-hoc env flags",
        "near-miss: env feature flags are banned outside local experiments",
        [
            "who owns runtime toggles",
            "where do we put feature flags",
            "feature flag service",
            "launchdarkly standing decision",
            "no ad-hoc env toggles",
        ],
    ),
    (
        "vite",
        "decision: migrated the web app bundler to vite from webpack",
        "near-miss: webpack remains only in one frozen legacy package",
        [
            "frontend build tooling",
            "vite not webpack",
            "web bundler decision",
            "migrated bundler choice",
            "how we build the frontend",
        ],
    ),
    (
        "loki",
        "decision: ship structured logs to self-hosted loki via promtail never default SaaS log sink",
        "near-miss: cloud only logging vendors are banned as default",
        [
            "logging destination",
            "stop suggesting cloud only logging",
            "loki promtail path",
            "structured logs destination",
            "self-hosted log stack",
        ],
    ),
    (
        "bearer",
        "decision: all endpoints require a bearer token from the auth service",
        "near-miss: cookie-only session auth was rejected for service APIs",
        [
            "api authentication method",
            "bearer token requirement",
            "how services authenticate HTTP",
            "auth standing decision for APIs",
            "no open endpoints policy",
        ],
    ),
    (
        "alembic",
        "decision: schema changes go through alembic revisions",
        "near-miss: raw SQL migrations in prod are banned",
        [
            "database migration tool",
            "alembic for schema",
            "how schema changes land",
            "migration standing decision",
            "stop hand-written prod SQL migrations",
        ],
    ),
    (
        "redis_cache",
        "decision: redis fronts the read-heavy queries",
        "near-miss: memcached is not used; redis owns cache",
        [
            "caching layer",
            "redis not memcached",
            "read-heavy query cache",
            "cache technology choice",
            "fronting cache decision",
        ],
    ),
    (
        "gha",
        "decision: github actions builds and tests every push",
        "near-miss: jenkins remains only on one abandoned repo",
        [
            "ci pipeline runner",
            "github actions standing decision",
            "where CI runs",
            "build on every push",
            "CI system choice",
        ],
    ),
    (
        "bash_ops",
        "decision: all ops scripts assume bash not zsh or fish",
        "near-miss: zsh is fine on laptops; servers stay on bash",
        [
            "preferred shell on servers",
            "ops scripts shell",
            "bash not fish",
            "server shell assumption",
            "shell standing decision for ops",
        ],
    ),
    (
        "celery",
        "decision: use a task queue with celery workers not threads or asyncio fire-and-forget",
        "near-miss: celery beat schedules periodic tasks; not a substitute for the worker pool decision",
        [
            "how background work is scheduled without threads",
            "how should I run background jobs",
            "celery not threads",
            "task queue standing decision",
            "background jobs mechanism",
        ],
    ),
    (
        "oauth_cookie",
        "session note: OAuth callback state mismatch after SameSite cookie change; fixed by aligning redirect cookie flags",
        "near-miss: OAuth login UI copy was redesigned last quarter unrelated to cookie flags",
        [
            "what broke login after the oauth refactor",
            "oauth state mismatch root cause",
            "SameSite cookie login bug",
            "callback state after oauth change",
            "login fail after cookie flags",
        ],
    ),
    (
        "harness_def",
        "in our setup harness means the Claude Code / Grok plugin runner not livestock or test harnesses",
        "near-miss: harness also means a horse collar in the style guide joke channel",
        [
            "operator meaning of harness in this repo",
            "what does harness mean here",
            "harness definition local",
            "plugin runner called harness",
            "harness not livestock",
        ],
    ),
    (
        "force_push_ban",
        "ban: force-push to main is forbidden; use revert commits or a new PR",
        "near-miss: force-push to personal feature branches is allowed with care",
        [
            "do not use force push on main",
            "force-push main ban",
            "git ban on shared main",
            "how to undo bad main commit",
            "never force push main",
        ],
    ),
    (
        "env_example_ban",
        "ban: .env.example must contain placeholders only; real secrets live in vault",
        "near-miss: committed real secrets in history must be rotated not just deleted",
        [
            "never put secrets in repo env samples",
            ".env.example policy",
            "secrets in sample env files",
            "placeholder only env examples",
            "ban on secrets in repo samples",
        ],
    ),
    (
        "mcp_live_enum",
        "correction: enumerate live MCP tools each session; do not assume servers from a static list",
        "near-miss: toolbox markdown lists may drift from connected servers",
        [
            "quit inventing MCP servers that are not connected",
            "enumerate live MCP tools",
            "static MCP list is wrong",
            "connected servers only",
            "do not invent MCP capabilities",
        ],
    ),
    (
        "verify_before",
        "standing rule: verify before announce; mark provisional when unconfirmed",
        "near-miss: momentum bias calls fixed before the fix holds",
        [
            "verify before asserting status",
            "provisional when unconfirmed",
            "do not call fixed too early",
            "honesty discipline standing rule",
            "check live state before report",
        ],
    ),
    (
        "reuse_before_build",
        "standing rule: reuse-before-build; do not parallel invent when existing tool fits",
        "near-miss: greenfield rebuilds without checking inventory are discouraged",
        [
            "reuse before build rule",
            "do not invent parallel tools",
            "check inventory first",
            "standing rule against parallel builds",
            "prefer existing path",
        ],
    ),
    (
        "four_ds",
        "standing rule: Four Ds filter — Dumb Dangerous Difficult Different — any hit reconsider",
        "near-miss: Different alone is not enough reason to invent a new stack",
        [
            "four ds filter meaning",
            "Dumb Dangerous Difficult Different",
            "when to reconsider an approach",
            "four ds standing rule",
            "overcomplicating filter",
        ],
    ),
    (
        "kiss",
        "standing rule: KISS — if you cannot explain in one sentence simplify",
        "near-miss: clever complexity is a smell not a goal",
        [
            "keep it simple standing rule",
            "one sentence explanation test",
            "KISS principle here",
            "simplify when explanation fails",
            "anti clever complexity",
        ],
    ),
    (
        "white_hat",
        "standing rule: white hat engineering; no shortcuts that bypass safety checks like --no-verify",
        "near-miss: --no-verify to silence hooks is forbidden as a default path",
        [
            "no bypass safety checks",
            "ban on --no-verify shortcuts",
            "white hat operational discipline",
            "do not skip hooks by default",
            "safety check standing rule",
        ],
    ),
    (
        "match_scope",
        "standing rule: match scope to what was asked; no drive-by refactors with a bug fix",
        "near-miss: drive-by cleanups in bugfix PRs are discouraged",
        [
            "no drive-by refactors",
            "match scope to the ask",
            "bug fix scope discipline",
            "do not bundle unrelated cleanups",
            "scope standing rule",
        ],
    ),
    (
        "definition_done",
        "standing rule: untested code is broken; prove with real check; persist fix to source control",
        "near-miss: live-only edits silently revert on next deploy",
        [
            "definition of done testing",
            "persist fix to source control",
            "live only edit is a countdown",
            "prove it works gate",
            "untested equals broken",
        ],
    ),
    (
        "subagent_review",
        "standing rule: do not review your own work in the same context; spawn a fresh reviewer",
        "near-miss: same-context self-review sees intent not text",
        [
            "when to spawn a fresh reviewer instead of self-check",
            "independent review rule",
            "fresh context reviewer",
            "do not self review same window",
            "subagent review discipline",
        ],
    ),
    (
        "refute_first",
        "standing rule: try to refute a finding before trusting it; default to refuted when uncertain",
        "near-miss: untested assertions need designed experiments not debate",
        [
            "adversarial verify findings",
            "refute before trust",
            "default to refuted when uncertain",
            "refuter agent purpose",
            "do not trust unrefuted claims",
        ],
    ),
    (
        "gpu_num",
        "decision: ollama options set num_gpu 999 to offload all layers when GPU present",
        "near-miss: CPU-only refine is supported but slower; num_gpu still set for when GPU appears",
        [
            "GPU offload for ollama layers",
            "num_gpu 999 meaning",
            "hammer GPU on embed and chat",
            "all layers offload setting",
            "TOTAL_RECALL_OLLAMA_NUM_GPU",
        ],
    ),
    (
        "batch_embed",
        "decision: embed batch default and num_batch 512 for throughput on 0.6b Q8",
        "near-miss: tiny batch sizes waste GPU on short chunk backfills",
        [
            "embed batch size product default",
            "num_batch for ollama embed",
            "throughput knobs for dense backfill",
            "batch embed options",
            "how large embed batches",
        ],
    ),
    (
        "chunk_size",
        "decision: chunk_for_embedding defaults around 400 tokens with overlap 50 sentence aware",
        "near-miss: stuffing 32k into one chunk dilutes attention and hurts retrieval",
        [
            "chunk size for embedding",
            "sentence aware chunking",
            "overlap tokens on chunks",
            "do not embed full 32k as one chunk",
            "max_tokens chunk_for_embedding",
        ],
    ),
    (
        "rrf_k",
        "decision: RRF uses k=60 per Cormack Clarke Buettcher standard default",
        "near-miss: ad-hoc weighted sum of uncalibrated scores is avoided",
        [
            "RRF k constant",
            "why reciprocal rank fusion k 60",
            "score free fusion reason",
            "RRF paper default k",
            "combine FTS and dense ranks",
        ],
    ),
    (
        "weighted_rrf",
        "decision: weighted_rrf mode available with dense weight default 3x FTS via env",
        "near-miss: weighted_rrf is available but not the product default",
        [
            "weighted rrf option",
            "TOTAL_RECALL_HYBRID_DENSE_WEIGHT",
            "3x dense weight fusion",
            "alternative to dense_primary",
            "weighted_rrf hybrid mode",
        ],
    ),
    (
        "vec_opt_out",
        "decision: TOTAL_RECALL_VEC=0 skips dense and embed pull; FTS-only remains",
        "near-miss: full opt-out also needs TOTAL_RECALL_LLM_PROVIDER=none for chat",
        [
            "how to disable dense embeds",
            "TOTAL_RECALL_VEC opt out",
            "FTS only mode",
            "skip embed pull",
            "disable vector layer",
        ],
    ),
    (
        "llm_opt_out",
        "decision: TOTAL_RECALL_LLM_PROVIDER=none disables chat refine only not embeds",
        "near-miss: provider none does not stop dense hybrid",
        [
            "disable LLM refine only",
            "TOTAL_RECALL_LLM_PROVIDER none",
            "chat refine off",
            "keep dense without refine",
            "opt out of qwen3.5 refine",
        ],
    ),
    (
        "schema_format",
        "decision: pass full JSON Schema as format for constrained decode not format json alone",
        "near-miss: format json only lets keys drift on small models",
        [
            "structured output schema for ollama",
            "format schema not bare json",
            "constrained decoding for refine",
            "JSON Schema in generate_json",
            "why schema in format field",
        ],
    ),
    (
        "batch_cap",
        "decision: refine batches cap around 25 entities so 2b list fidelity holds",
        "near-miss: 50+ field deep schemas degrade small models",
        [
            "batch size for refine_machines",
            "list fidelity limit on 2b",
            "why chunk machines refine",
            "max entities per LLM call",
            "small model batch discipline",
        ],
    ),
    (
        "echo_filter",
        "decision: client rejects outputs that are near-verbatim of input after refine",
        "near-miss: constrained decode does not guarantee grounded extract",
        [
            "post validation after refine",
            "reject unknown keys after JSON",
            "grounded extract still need filters",
            "echo filter after generate_json",
            "do not trust format alone",
        ],
    ),
    (
        "english_instruct",
        "decision: embed instruct task text is English even for non-English corpora per card",
        "near-miss: non-English instruct strings are off-distribution for Qwen embed training",
        [
            "instruct language for embeds",
            "English task line even if docs other languages",
            "card guidance on instruct language",
            "do not freestyle encode this search query prefixes",
            "official Instruct Query template",
        ],
    ),
    (
        "rebuild_identity",
        "decision: change of model backend or dim forces dense rebuild; query instruct change does not",
        "near-miss: changing only query instruct does not require re-embed because docs stay raw",
        [
            "rebuild after model identity change",
            "when re-embed is required",
            "instruct change needs rebuild?",
            "dim mismatch forces rebuild",
            "identity keys in vec_meta",
        ],
    ),
    (
        "hooks_timeout",
        "decision: async re-index hooks keep timeout 60; fast hooks use 88plug short timeouts",
        "near-miss: killing Stop mid-write corrupts index; timeout 60 is intentional",
        [
            "Stop hook timeout reason",
            "async reindex timeout 60",
            "fast hook timeout budget",
            "SessionStart timeout 15",
            "why Stop is not killed at 10s",
        ],
    ),
    (
        "privacy_local",
        "decision: transcripts never leave the machine; refine prompts stay local ollama only",
        "near-miss: cloud LLM refine is not the product path",
        [
            "privacy for transcript content",
            "local only refine",
            "do transcripts leave the machine",
            "ollama local privacy note",
            "no cloud refine default",
        ],
    ),
    (
        "mcp_tools_count",
        "decision: product surfaces 26 MCP tools plus 6 hooks 15 slash commands 3 skills",
        "near-miss: tool counts in stale docs must match plugin.json",
        [
            "how many MCP tools total-recall has",
            "MCP tools hooks commands skills counts",
            "plugin surface area",
            "26 tools meaning",
            "slash command count",
        ],
    ),
    (
        "sources_10",
        "decision: mines transcripts from 10 CLI clients including Claude Code Cursor Codex Gemini Goose Grok",
        "near-miss: marketplace path is Claude-Code-only; other CLIs use their MCP config",
        [
            "how many CLI session sources",
            "which CLIs are indexed",
            "cross CLI memory support",
            "Goose Grok session mining",
            "10 session sources",
        ],
    ),
    (
        "fsl_license",
        "decision: license is FSL-1.1-ALv2 on the plugin",
        "near-miss: do not paste full license text into chat when creating LICENSE files",
        [
            "plugin license",
            "FSL license total-recall",
            "what license does the plugin use",
            "FSL-1.1-ALv2",
            "license standing fact",
        ],
    ),
    (
        "author_88plug",
        "decision: author is 88plug with email andrew@88plug.com in plugin manifest",
        "near-miss: marketplace name is 88plug not total-recall org",
        [
            "plugin author",
            "88plug author email",
            "who publishes total-recall marketplace",
            "manifest author field",
            "andrew@88plug.com",
        ],
    ),
]


def _expand_pairs() -> list[tuple[str, str, str, str]]:
    """Return list of (family, query, target, near_miss)."""
    out: list[tuple[str, str, str, str]] = []
    for sid, target, near, templates in _SEEDS:
        for q in templates:
            out.append((sid, q, target, near))
    return out


SOFT: list[str] = [
    "the lab coffee grinder needs burrs replaced",
    "someone reserved the conference room for yoga",
    "the parking lot lights flicker after midnight",
    "bring a dish for potluck friday",
    "the 3d printer is offline pending nozzle",
    "plant watering rota is on the fridge",
    "board game night is every other thursday",
    "the HVAC filter was replaced in march",
    "standup is 10am in the lab",
    "the office wifi password rotates monthly",
    "parking validation is at the front desk",
    "holiday party is the second week of december",
    "lunch is catered on fridays",
    "the logo uses the brand teal",
    "remember to expense the conference tickets",
]


# Extra pure symbol adversarial (exact id vs near-id)
SYMBOL_EXTRA: list[tuple[str, str, str]] = [
    ("web-01", "ops: nginx restarted on web-01 after certificate rotation", "ops: web-02 was decommissioned last year"),
    ("web-02", "ops: web-02 still serves canary traffic on port 8443", "ops: nginx restarted on web-01 after certificate rotation"),
    ("gpu-box-3", "ssh gpu-box-3 for nvidia-smi after the driver bump", "gpu-box-2 is drained for maintenance"),
    ("edge-relay", "caddy on edge-relay reloaded certs", "edge-relay-old was removed from DNS"),
    ("qwen3-embedding:0.6b", "product embed model tag is qwen3-embedding:0.6b", "embeddinggemma:300m exists as manual override only"),
    ("qwen3.5:2b", "DEFAULT_MODEL for refine is qwen3.5:2b", "qwen3.5:9b lost the bakeoff"),
    ("TOTAL_RECALL_EMBED_KEEP_ALIVE", "env TOTAL_RECALL_EMBED_KEEP_ALIVE defaults to -1", "TOTAL_RECALL_LLM_KEEP_ALIVE pins chat not embed"),
    ("TOTAL_RECALL_VEC", "TOTAL_RECALL_VEC=0 skips dense", "TOTAL_RECALL_LLM_PROVIDER=none skips chat only"),
    ("inject-claudemd-into-subagents.sh", "hook path inject-claudemd-into-subagents.sh on SubagentStart", "other hooks do not re-inject CLAUDE.md"),
    ("NullPointerException", "incident: NullPointerException in auth middleware after SameSite change", "NullPointerException in an unrelated batch job last year"),
    ("192.168.1.211:8890", "searxng first hop is 192.168.1.211:8890", "tailscale backend is 100.113.242.91:8890 as last resort"),
    ("format=2", "vec_meta format=2 is ollama-only dense", "format=1 indexes must rebuild"),
]


@dataclass
class RankMetrics:
    p1: float = 0.0
    p5: float = 0.0
    mrr: float = 0.0
    n: int = 0
    misses: list[str] = field(default_factory=list)

    def add(self, ranks: list[str], target: str) -> None:
        self.n += 1
        if target in ranks[:1]:
            self.p1 += 1
        else:
            self.misses.append(target[:70])
        if target in ranks[:5]:
            self.p5 += 1
        try:
            self.mrr += 1.0 / (ranks.index(target) + 1)
        except ValueError:
            pass

    def fin(self) -> dict:
        n = max(self.n, 1)
        return {
            "n": self.n,
            "p@1": round(self.p1 / n, 4),
            "p@5": round(self.p5 / n, 4),
            "mrr": round(self.mrr / n, 4),
            "miss_rate@1": round(1 - self.p1 / n, 4),
            "miss_samples": self.misses[:5],
        }


def _stamp(conn, kind, content, cwd, ts, uuid, score):
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


def build_and_run() -> dict:
    from index.db import connect
    from vec.embed import Embedder
    from vec.rrf import hybrid_search
    from vec.runtime import ensure_product_ollama
    from vec.store import apply_vec_schema, backfill_all, vec_search

    os.environ.pop("TOTAL_RECALL_EMBED_INSTRUCT", None)
    ensure_product_ollama(embed=True, pull=True)
    emb = Embedder()
    emb._load()

    pairs = _expand_pairs()  # (family, query, target, near)
    # symbols as family "symbol"
    for q, t, n in SYMBOL_EXTRA:
        pairs.append(("symbol", q, t, n))

    n_pairs = len(pairs)
    print(f"labeled pairs: {n_pairs}", flush=True)

    cwd = "/proj/adv10x"
    tmp = Path(tempfile.mkdtemp()) / "adv10x.db"
    conn = connect(tmp)
    ts = 1_750_000_000
    seen_docs: set[str] = set()
    i = 0
    for _fam, _q, target, near in pairs:
        if target not in seen_docs:
            kind = "ban" if target.startswith("ban:") else (
                "correction" if target.startswith("correction:") else "decision"
            )
            _stamp(conn, kind, target, cwd, ts + i, f"t{i}", 0.85)
            seen_docs.add(target)
            i += 1
        if near not in seen_docs:
            _stamp(conn, "domain_fact", near, cwd, ts + 5000 + i, f"n{i}", 0.4)
            seen_docs.add(near)
            i += 1
    for j, d in enumerate(SOFT):
        _stamp(conn, "domain_fact", d, cwd, ts + 9000 + j, f"s{j}", 0.3)
    conn.commit()

    apply_vec_schema(
        conn, dim=emb.dim(), model=emb.model or "qwen3-embedding:0.6b",
        backend=emb.backend or "ollama",
    )
    t0 = time.perf_counter()
    rep = backfill_all(conn, embedder=emb)
    backfill_s = time.perf_counter() - t0
    print(
        f"backfill embedded={rep.extractions_embedded} chunks={rep.chunks_written} "
        f"s={backfill_s:.2f}",
        flush=True,
    )

    by_family: dict[str, dict[str, RankMetrics]] = defaultdict(
        lambda: {"pure": RankMetrics(), "fts": RankMetrics(), "hyb": RankMetrics()}
    )
    overall = {"pure": RankMetrics(), "fts": RankMetrics(), "hyb": RankMetrics()}

    t_search0 = time.perf_counter()
    for idx, (fam, query, target, _near) in enumerate(pairs):
        pure = _contents(vec_search(conn, query, embedder=emb, limit=10, cwd=cwd))
        fts = _contents(hybrid_search(conn, query, embedder=None, limit=10, cwd=cwd))
        hyb = _contents(hybrid_search(conn, query, embedder=emb, limit=10, cwd=cwd))
        for bucket, ranks in (("pure", pure), ("fts", fts), ("hyb", hyb)):
            by_family[fam][bucket].add(ranks, target)
            overall[bucket].add(ranks, target)
        if (idx + 1) % 50 == 0:
            print(f"  scored {idx+1}/{n_pairs}", flush=True)
    search_s = time.perf_counter() - t_search0
    conn.close()

    family_report = {}
    for fam, mets in sorted(by_family.items()):
        family_report[fam] = {
            "pure_dense": mets["pure"].fin(),
            "fts_only": mets["fts"].fin(),
            "hybrid": mets["hyb"].fin(),
        }

    ov = {
        "pure_dense": overall["pure"].fin(),
        "fts_only": overall["fts"].fin(),
        "hybrid": overall["hyb"].fin(),
    }

    # Worst families by hybrid miss rate
    worst = sorted(
        (
            (fam, family_report[fam]["hybrid"]["miss_rate@1"], family_report[fam]["hybrid"]["p@1"])
            for fam in family_report
        ),
        key=lambda x: -x[1],
    )[:15]

    hyb, pure, fts = ov["hybrid"], ov["pure_dense"], ov["fts_only"]
    gates = {
        "n_pairs_ge_400": n_pairs >= 400,
        "hybrid_p@1_ge_0.55": hyb["p@1"] >= 0.55,
        "hybrid_p@5_ge_0.8": hyb["p@5"] >= 0.8,
        "hybrid_mrr_ge_0.65": hyb["mrr"] >= 0.65,
        "hybrid_ge_dense_p@1": hyb["p@1"] + 1e-9 >= pure["p@1"],
        "hybrid_ge_fts_p@1": hyb["p@1"] + 0.02 >= fts["p@1"],
        "hybrid_best_of_three_p@1": hyb["p@1"] >= pure["p@1"] and hyb["p@1"] >= fts["p@1"],
        "hybrid_best_of_three_mrr": hyb["mrr"] >= pure["mrr"] and hyb["mrr"] >= fts["mrr"],
        "miss_rate_le_0.45": hyb["miss_rate@1"] <= 0.45,
        "symbol_hybrid_p@1_ge_0.75": family_report.get("symbol", {}).get("hybrid", {}).get("p@1", 0) >= 0.75,
        "dense_beats_random": pure["p@1"] >= 0.25,  # far above 1/n chance
    }

    return {
        "model": emb.model,
        "dim": emb.dim(),
        "query_instruct": (emb._query_prefix or "")[:100],
        "n_pairs": n_pairs,
        "n_seeds": len(_SEEDS),
        "n_docs_indexed": len(seen_docs) + len(SOFT),
        "backfill": {
            "embedded": rep.extractions_embedded,
            "chunks": rep.chunks_written,
            "seconds": round(backfill_s, 3),
        },
        "search_seconds": round(search_s, 2),
        "overall": ov,
        "worst_families": [
            {"family": f, "miss_rate@1": m, "p@1": p} for f, m, p in worst
        ],
        "families": family_report,
        "gates": gates,
    }


def render(report: dict) -> str:
    lines = [
        "# Adversarial 10× eval",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        f"**n_pairs={report['n_pairs']}** seeds={report['n_seeds']} "
        f"docs={report['n_docs_indexed']} model=`{report['model']}`",
        "",
        "## Overall",
        "```json",
        json.dumps(report["overall"], indent=2),
        "```",
        "",
        "## Worst families (by hybrid miss@1)",
        "```json",
        json.dumps(report["worst_families"], indent=2),
        "```",
        "",
        "## Gates",
    ]
    for k, v in report["gates"].items():
        lines.append(f"- `{'PASS' if v else 'FAIL'}` {k}")
    ok = all(report["gates"].values())
    lines.append("")
    lines.append(
        f"**Overall: {'PASS' if ok else 'FAIL'}** "
        f"({sum(report['gates'].values())}/{len(report['gates'])})"
    )
    lines.append("")
    lines.append("<details><summary>Per-family metrics</summary>")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report["families"], indent=2))
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/eval-adversarial-10x.md"))
    args = ap.parse_args()

    print("=== adversarial 10x ===", flush=True)
    report = build_and_run()
    print(json.dumps({
        "n_pairs": report["n_pairs"],
        "overall": report["overall"],
        "worst_families": report["worst_families"],
        "gates": report["gates"],
        "backfill": report["backfill"],
        "search_seconds": report["search_seconds"],
    }, indent=2), flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report), encoding="utf-8")
    print(f"Wrote {args.out}", flush=True)
    return 0 if all(report["gates"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
