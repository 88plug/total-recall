<div align="center">

# Total Recall

Cross-session memory for Claude Code and AI coding assistants — mines your own transcripts so new sessions already know decisions, bans, corrections, and goals.

[![plugin-validate](https://github.com/88plug/total-recall/actions/workflows/plugin-validate.yml/badge.svg)](https://github.com/88plug/total-recall/actions/workflows/plugin-validate.yml)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue?style=flat)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-online-2ea44f?style=flat)](https://88plug.github.io/total-recall/)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2?style=flat)](https://github.com/88plug/claude-code-plugins)
[![DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/88plug/total-recall)

</div>

Total Recall is a Claude Code plugin and MCP server that turns session JSONL into a local knowledge graph and RAG index. It mines transcripts across Claude Code and 9 other AI CLIs, then surfaces operator identity, decisions, bans, corrections, goals, and voice via 26 MCP tools, 6 hooks, 15 slash commands, and 3 skills.

Hybrid FTS5 + ollama dense embeddings (`qwen3-embedding:0.6b`) power recall. Worktree-aware project scoping and a post-compaction coding-continuity packet keep context alive. Everything stays on your machine — no cloud embed APIs, no re-upload of transcripts.

## Install

```text
/plugin marketplace add 88plug/claude-code-plugins
/plugin install total-recall@88plug
```

> [!NOTE]
> Needs `bash` + `curl` + internet. The plugin bootstraps `uv`, Python, and deps into its own data dir on first hook fire. No system-wide `pip install` and no system Python required.

## Quickstart

First run backfills existing transcripts in the background (detached; progress in `logs/bootstrap.log`). New sessions get a short SessionStart brief for the current directory.

```text
/recall-status
/recall what did we decide about the deploy pipeline?
```

Full reindex:

```bash
total-recall index --rebuild --jobs 4
```

A typical corpus drops from ~22s single-threaded to ~9s at `--jobs 4`.

## Features

| Area | What you get |
|---|---|
| Cross-session memory | Decisions, corrections, bans, goals, progress, domain facts from past sessions |
| Cross-CLI RAG | One index across 10 clients (Claude Code, OpenCode, Codex, Gemini CLI, Cursor, Continue, Cline, Aider, Goose, Grok) |
| MCP + hooks | 26 live tools, 6 hooks (SessionStart, retrieve, re-index, Pre/PostCompact continuity) |
| Operator discovery | Profile, voice, ontology, workflow, implicit prefs, satisfaction — data-driven from your transcripts |
| Local embeddings | SQLite FTS5 + ollama vectors; product-owned binary under plugin data dir |
| Metrics | Tokens, cost, topics, health — all from the local index, no telemetry |

## Why this exists

Claude Code has three kinds of memory today, and none mine transcript history: `amnesia` (88plug) keeps one session alive across compaction but ignores other sessions; auto-memory is hand-curated; `CLAUDE.md` is static.

The operator is the source of truth. Models and projects change; you do not. Total Recall makes that explicit: operator profile and voice profile are first-class artifacts, queryable in one MCP call at session start.

## What it captures

17 extractors total. 11 run inline over each session; 6 are operator-level aggregators over the full corpus.

<details>
<summary>Per-session extractors (11)</summary>

- `corrections` — turns where you redirected the model
- `decisions` — "we're going with X because Y"
- `self_corrections` — model self-corrections ("actually, scratch that")
- `progress` — how far a line of work got
- `domain_facts` — durable codebase/env signals (versions, paths, conventions)
- `away_summaries` — recap text after returning to a stale session
- `model_corrections` — corrections about model behavior or output format
- `standing_decisions` — decisions marked durable across sessions
- `bans` — explicit "never do X"
- `goals` — what you said you are trying to achieve
- `truth_rhetoric` — assertions about objective state, for later checks

</details>

<details>
<summary>Operator-level extractors (6)</summary>

- `operator_profile` — who you are, how you work, cross-project preferences
- `voice_profile` — tone, phrasing, verbal tics for register matching
- `ontology` — your vocabulary for projects/machines/services + co-mention graph
- `workflow` — fan-out vocabulary, autonomy score, interrupt rate, planning idiom, work window, subagent adoption
- `implicit_preferences` — preferences from behavior, promoted past a multi-axis threshold
- `satisfaction` — praise/frustration profile paired with preceding assistant-turn shape

</details>

## Reference

### MCP tools (26)

Live queries the model can call mid-conversation.

<details>
<summary>Full tool list</summary>

Core recall:

- `recall`
- `recall_targeted`
- `prior_sessions_for_cwd`
- `get_session_digest`
- `search_messages`
- `find_failed_attempts`
- `list_failed_attempts`
- `find_user_preferences`

Operator-aware:

- `get_operator_context`
- `get_operator_profile`
- `get_voice_profile`
- `recall_corrections_about`
- `get_recent_corrections`
- `list_standing_decisions`
- `get_decision_for_topic`
- `check_banned`
- `get_active_goal`
- `list_goals`
- `get_past_truth_assertions`
- `assess_escalation_risk`
- `get_project_graph`
- `get_machine_inventory`
- `define_term`

Workflow, satisfaction, implicit prefs:

- `get_workflow_profile`
- `get_satisfaction_profile`
- `list_implicit_preferences`

Recommended one-call pattern: `get_operator_context` (operator profile, voice, active goal, recent corrections, standing decisions).

</details>

### Hooks

Each hook is registered in `hooks/hooks.json` and is independently disable-able.

<details>
<summary>Hook list</summary>

- SessionStart (startup/clear) — `session-start-signpost.sh`: budget-aware signpost for prior sessions in this directory
- SessionStart (compact) — `session-start-compact-restore.sh`: restore continuity after compaction-triggered start
- UserPromptSubmit (async) — `user-prompt-retrieve.sh`: fetch highly relevant memories on demand
- Stop (async) — `stop-index.sh`: re-index new turns
- PreCompact — `pre-compact-seed.sh`: seed a coding-continuity packet before compaction
- PostCompact — `post-compact-recovery.sh` and `post-compact-index.sh` (async): recover continuity and re-index

</details>

### Slash commands (15)

<details>
<summary>Command list</summary>

- `/recall` — query your memory
- `/recall-status` — index and ingest status
- `/recall-inspect` — inspect extracted records
- `/recall-rebuild` — full reindex
- `/recall-promote` — promote a signal to a standing decision
- `/recall-operator-context` — bundled operator context
- `/recall-corrections` — list corrections
- `/recall-decisions` — list decisions
- `/recall-goal` — active goal
- `/recall-check-banned` — check banned actions
- `/recall-escalation` — escalation-risk assessment
- `/recall-metrics` — usage metrics summary
- `/recall-cost` — per-model token and cost breakdown
- `/recall-topics` — most-extracted topics
- `/recall-health` — ingest age, hook fire rate, latency, errors

</details>

### Skills (3)

- `/recall` — orientation guidance for deeper dives
- `/speak-like-operator` — voice-matching skill, filled from `get_voice_profile()`
- `/total-recall:llm-setup` — manual fallback for local-LLM provisioning

### Cross-CLI sources

One index spans 10 clients: Claude Code, OpenCode, Codex CLI, Gemini CLI, Cursor, Continue, Cline, Aider, Goose, and Grok. Cross-source dedup keeps the highest-priority copy of duplicated turns.

## Metrics

`total-recall metrics` reports tokens spent, slowest sessions, most-corrected topics, and compaction frequency from the local SQLite index. No external collector, no telemetry, no SaaS.

<details>
<summary>Metrics subcommands</summary>

- `total-recall metrics summary [--since 7d] [--project PATH]` — sessions, tokens (cache-read %), wall vs active hours, cost, top corrections, busiest project, longest session
- `total-recall metrics cost [--rate model=in/out] [--since 30d]` — per-model token and cost breakdown
- `total-recall metrics sessions [--top 10] [--by tokens|duration|corrections]` — rank sessions
- `total-recall metrics topics [--since 30d] [--limit 10]` — most-extracted topics
- `total-recall metrics health` — last ingest age, hook fire rate, p95 latency, error count

All subcommands support `--json`.

</details>

## Storage and privacy

Everything stays under `${CLAUDE_PLUGIN_DATA}/total-recall/` (env-resolved by Claude Code; do not hardcode). Holds the SQLite index (`index.db`, FTS5 + dense vectors), optional managed `bin/ollama`, `state.json` offsets, and rotating logs. Session JSONLs are never written to.

> [!NOTE]
> Read-only on `~/.claude/projects/*.jsonl`, local-only, no re-uploading. Transcripts hold secrets and private code — they never leave the machine. Dense embeddings use product-owned ollama (auto-provisioned under the plugin data dir, `qwen3-embedding:0.6b`); no cloud embed APIs and no in-process ONNX/fastembed path.

## Optional local-LLM refinement

On first install, Total Recall sets up a small local model (`qwen3.5:2b`) in the background. Bootstrap fetches the ollama binary (~38 MB, no `sudo`, into the plugin data dir) and pulls the default model (~2.7 GB). A one-time banner announces setup in progress.

The model runs on-device via ollama; transcripts are never uploaded. Cloud APIs are not supported (would break the no-reupload guarantee). Refinement runs on the cold path only (`rebuild`); if ollama is not ready, the heuristic baseline runs instead.

<details>
<summary>What refinement improves</summary>

| What gets refined | Heuristic baseline | With qwen3.5:2b |
|---|---|---|
| Machine-name extraction | Pattern-based NER | Precision 1.0, recall 1.0 |
| Vocabulary definitions | Absent | About 60% coverage |
| Project narratives | None | Short, accurate summaries |

</details>

<details>
<summary>Environment variables</summary>

| Env var | Default | Description |
|---|---|---|
| `TOTAL_RECALL_LLM_PROVIDER` | `auto` | `none` disables the LLM layer; `ollama` forces ollama |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Override model; larger = more coverage, more RAM |
| `TOTAL_RECALL_LLM_REFINE_TEXT` | `1` | `0` disables text-gen refinement, keeps machine-name extraction |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://127.0.0.1:11435` | Product ollama API (not system :11434) |

</details>

To disable everything: `TOTAL_RECALL_LLM_PROVIDER=none` before the plugin starts. `/total-recall:llm-setup` is the manual fallback if auto-provisioning fails. See [`docs/llm-refinement.md`](docs/llm-refinement.md).

## Relation to amnesia

`amnesia` and `total-recall` are complements. `amnesia` owns current working state within one session across compaction; `total-recall` owns the historical record across sessions and projects. If `amnesia` is installed, `total-recall` reads its `memory/` snapshots as a high-signal extra source without duplicating or overwriting them.

## Development

```bash
git clone https://github.com/88plug/total-recall.git
cd total-recall
uv sync --all-groups    # or: pip install -e ".[dev]"
uv run ruff check .
uv run pytest
uv run mkdocs build --strict
claude --plugin-dir "$PWD"
```

Docs: [https://88plug.github.io/total-recall/](https://88plug.github.io/total-recall/). Architecture: [`docs/architecture.md`](docs/architecture.md).

## License

[Functional Source License, Version 1.1, ALv2 Future License](LICENSE) (`FSL-1.1-ALv2`).

Free to use, copy, modify, and redistribute for any purpose except a Competing Use — offering this software (or a substantially similar substitute) as a commercial product or service. Each released version converts to Apache License 2.0 on the second anniversary of its release date. Commercial-use inquiries outside the Permitted Purpose: andrew@88plug.com.
