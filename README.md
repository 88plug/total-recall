<div align="center">

# Total Recall

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/88plug/total-recall)

Cross-session, cross-CLI memory for Claude Code and other AI coding assistants — it mines your own session transcripts so a new session already knows your decisions, corrections, bans, and goals.

[![plugin-validate](https://github.com/88plug/total-recall/actions/workflows/plugin-validate.yml/badge.svg)](https://github.com/88plug/total-recall/actions/workflows/plugin-validate.yml)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue?style=flat)](LICENSE.md)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2?style=flat)](https://github.com/88plug/claude-code-plugins)
[![Docs](https://img.shields.io/badge/docs-online-2ea44f?style=flat)](https://88plug.github.io/total-recall/)

</div>

Every session you run is already on disk as append-only JSONL: every decision, every "no, do it this way", every dead end. Total Recall reads that history locally and feeds the high-signal parts back to new sessions in a low-token form, so the model stops re-asking what you already told it.

## Install

Marketplace (recommended):

```text
/plugin marketplace add 88plug/claude-code-plugins
/plugin install total-recall@88plug
```

Local checkout (for development):

```bash
git clone https://github.com/88plug/total-recall.git
cd total-recall
pip install -e .[vec]
claude --plugin-dir "$PWD"
```

> [!NOTE]
> Requirements are `bash` + `curl` + internet. The plugin bootstraps everything else (`uv`, Python, deps) into its own data dir on first hook fire. No system-wide `pip install` and no system Python required.

## Quickstart

First run backfills your existing transcripts in the background (detached, so it survives the spawning session exiting; progress goes to `logs/bootstrap.log`). After that, every new session gets a short SessionStart brief about what is relevant to the current directory.

Check what was indexed:

```text
/recall-status
```

Ask the model to use its memory at any time:

```text
/recall what did we decide about the deploy pipeline?
```

For a manual full reindex:

```bash
total-recall index --rebuild --jobs 4
```

A typical corpus drops from about 22s single-threaded to about 9s at `--jobs 4`.

## Why this exists

Claude Code has three kinds of memory today, and none of them mine the transcript history: `amnesia` (88plug) keeps one session alive across compaction but ignores other sessions; auto-memory (`~/.claude/projects/<proj-slug>/memory/`) is hand-curated; `CLAUDE.md` is static and hand-edited.

The operator is the source of truth. Models change and projects come and go; the human running the sessions is the one constant. Total Recall makes that explicit: an operator profile and voice profile are first-class extracted artifacts, queryable in one MCP call at session start.

## What it captures

17 extractors total. 11 run inline over each session's record stream; 6 are operator-level aggregators that run out-of-band against the full corpus.

<details>
<summary>Per-session extractors (11)</summary>

- `corrections` — turns where you redirected the model.
- `decisions` — "we're going with X because Y" moments.
- `self_corrections` — places the model corrected itself ("actually, scratch that").
- `progress` — how far a line of work actually got; anchors "we already did X".
- `domain_facts` — durable signals about the codebase or environment (versions, paths, conventions).
- `away_summaries` — recap text you wrote after returning to a stale session.
- `model_corrections` — corrections specifically about model behavior or output format.
- `standing_decisions` — decisions you marked as durable across sessions.
- `bans` — explicit "never do X" instructions.
- `goals` — what you said you are trying to achieve in a session.
- `truth_rhetoric` — assertions you made about objective state, kept so a later session can check whether they still hold.

</details>

<details>
<summary>Operator-level extractors (6)</summary>

- `operator_profile` — durable signals about the human: who they are, how they work, preferences across projects.
- `voice_profile` — how you write: tone, phrasing patterns, verbal tics, so a model can match register without being told.
- `ontology` — vocabulary you use for your own systems (project, machine, and service names) plus a cross-project co-mention graph.
- `workflow` — how you work: fan-out vocabulary, autonomy score, mid-flight interrupt rate, planning idiom, preferred work window, subagent adoption.
- `implicit_preferences` — preferences expressed by behavior rather than as a ban or decision (tool-call ratios, shell-command dominance, format preferences). Promoted only past a multi-axis threshold.
- `satisfaction` — bidirectional praise/frustration profile paired with the preceding assistant-turn shape.

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

The recommended one-call pattern is `get_operator_context`, which bundles the operator profile, voice profile, active goal, recent corrections, and standing decisions.

</details>

### Hooks

Each hook is registered in `hooks/hooks.json` and is independently disable-able.

<details>
<summary>Hook list</summary>

- SessionStart (startup/clear) — `session-start-signpost.sh`: emits a tiny, budget-aware signpost pointing at prior sessions for this directory.
- SessionStart (compact) — `session-start-compact-restore.sh`: restores continuity after a compaction-triggered start.
- UserPromptSubmit (async) — `user-prompt-retrieve.sh`: fetches highly relevant memories on demand.
- Stop (async) — `stop-index.sh`: re-indexes new turns.
- PreCompact — `pre-compact-seed.sh`: seeds a coding-continuity packet before compaction.
- PostCompact — `post-compact-recovery.sh` and `post-compact-index.sh` (async): recover continuity and re-index after compaction.

</details>

### Slash commands (15)

For the human operator.

<details>
<summary>Command list</summary>

- `/recall` — query your own memory.
- `/recall-status` — index and ingest status.
- `/recall-inspect` — inspect extracted records.
- `/recall-rebuild` — full reindex.
- `/recall-promote` — promote a signal to a standing decision.
- `/recall-operator-context` — show the bundled operator context.
- `/recall-corrections` — list corrections.
- `/recall-decisions` — list decisions.
- `/recall-goal` — show the active goal.
- `/recall-check-banned` — check banned actions.
- `/recall-escalation` — escalation-risk assessment.
- `/recall-metrics` — usage metrics summary.
- `/recall-cost` — per-model token and cost breakdown.
- `/recall-topics` — most-extracted topics.
- `/recall-health` — ingest age, hook fire rate, latency, errors.

</details>

### Skills (3)

- `/recall` — orientation-style guidance the model loads on demand for deeper dives.
- `/speak-like-operator` — voice-matching skill, runtime-populated from `get_voice_profile()`.
- `/total-recall:llm-setup` — manual fallback for local-LLM provisioning.

### Cross-CLI sources

One index spans 8 supported clients: Claude Code, OpenCode, Codex CLI, Gemini CLI, Cursor, Continue, Cline, and Aider. Cross-source dedup keeps the highest-priority copy of duplicated turns.

## Metrics

After the index is built, `total-recall metrics` gives you visibility into your own usage — tokens spent, slowest sessions, most-corrected topics, compaction frequency — all from the local SQLite index. No external collector, no telemetry, no SaaS.

<details>
<summary>Metrics subcommands</summary>

- `total-recall metrics summary [--since 7d] [--project PATH]` — sessions, tokens (with cache-read %), wall vs active hours, estimated cost, top corrections, busiest project, longest session.
- `total-recall metrics cost [--rate model=in/out] [--since 30d]` — per-model token and cost breakdown using bundled default rates or your overrides.
- `total-recall metrics sessions [--top 10] [--by tokens|duration|corrections]` — rank sessions on a column.
- `total-recall metrics topics [--since 30d] [--limit 10]` — most-extracted topics across corrections and decisions.
- `total-recall metrics health` — last ingest age, hook fire rate, p95 latency, error count.

All subcommands support `--json`.

</details>

## Storage and privacy

Everything stays under `${CLAUDE_PLUGIN_DATA}/total-recall/` (env-resolved by Claude Code; do not hardcode the path). It holds the SQLite index (`index.db`, FTS5 for keyword recall), optional `vec.db` embeddings (only with the `[vec]` extra), `state.json` offsets, and rotating logs. The session JSONLs themselves are never written to.

> [!NOTE]
> Read-only on `~/.claude/projects/*.jsonl`, local-only, and no re-uploading. Transcripts contain secrets, internal URLs, and private code, so they never leave the machine. Embeddings, if enabled, run in-process via `fastembed`.

## Optional local-LLM refinement

On first install, Total Recall sets up a small local model (`qwen3.5:2b`) in the background — nothing for you to do. Bootstrap fetches the ollama binary (about 38 MB, no `sudo`, into the plugin data dir) and pulls the default model (about 2.7 GB). A one-time banner announces setup is in progress.

Everything stays on your machine: the model runs on-device via ollama, and transcripts are never uploaded. Cloud APIs are deliberately not supported, since they would break the no-reupload guarantee. Refinement runs on the cold path only (during `rebuild`); if ollama is not ready, the heuristic baseline runs instead and nothing breaks.

<details>
<summary>What refinement improves</summary>

| What gets refined | Heuristic baseline | With qwen3.5:2b |
|---|---|---|
| Machine-name extraction | Pattern-based NER | Precision 1.0, recall 1.0 |
| Vocabulary definitions | Absent | About 60% coverage |
| Project narratives | None | Short, accurate summaries |

</details>

### Configuration

<details>
<summary>Environment variables</summary>

| Env var | Default | Description |
|---|---|---|
| `TOTAL_RECALL_LLM_PROVIDER` | `auto` | `none` disables the entire LLM layer. `ollama` forces the ollama path. |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Override the model; larger models give higher coverage at more RAM and slower runs. |
| `TOTAL_RECALL_LLM_REFINE_TEXT` | `1` | Set to `0` to disable text-gen refinement while keeping machine-name extraction. |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://localhost:11434` | Ollama API endpoint. |

</details>

To disable everything, set `TOTAL_RECALL_LLM_PROVIDER=none` before the plugin starts. The `/total-recall:llm-setup` command is a manual fallback if auto-provisioning fails. See [`docs/llm-refinement.md`](docs/llm-refinement.md) for troubleshooting.

## Relation to amnesia

`amnesia` and `total-recall` are complements, not competitors. `amnesia` owns the current working state within one session across compaction; `total-recall` owns the historical record across sessions and projects. If `amnesia` is installed, `total-recall` reads its `memory/` snapshots as a high-signal extra source without duplicating or overwriting them.

## Contributing

```bash
pip install -e .[dev,vec]
ruff check .
mypy total_recall
pytest
```

The architecture is a flat 4-layer pipeline; see [`docs/architecture.md`](docs/architecture.md).

## License

[Functional Source License, Version 1.1, ALv2 Future License](LICENSE.md) (`FSL-1.1-ALv2`).

In plain English: free to use, copy, modify, and redistribute for any purpose except a Competing Use — offering this software (or a substantially similar substitute) as a commercial product or service. Each released version converts to the Apache License 2.0 on the second anniversary of its release date. For commercial-use inquiries outside the Permitted Purpose: claude@cryptoandcoffee.com.
