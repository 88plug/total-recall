# Total Recall

Memory & RAG for Claude Code and Grok. Mines your own session
transcripts so a new session already knows your decisions, corrections, bans, and goals.

[![plugin-validate](https://github.com/88plug/total-recall/actions/workflows/plugin-validate.yml/badge.svg)](https://github.com/88plug/total-recall/actions/workflows/plugin-validate.yml)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue?style=flat)](https://github.com/88plug/total-recall/blob/main/LICENSE)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2?style=flat)](https://github.com/88plug/claude-code-plugins)
[![PyPI](https://img.shields.io/badge/pypi-total--recall-blue?style=flat)](https://pypi.org/project/total-recall/)
[![Docs](https://img.shields.io/badge/docs-online-blue?style=flat)](https://88plug.github.io/total-recall/)

Every session is already on disk as append-only JSONL. Total Recall reads that history
locally and feeds the high-signal parts back in a low-token form. The model stops
re-asking what you already told it.

| Surface | What you get |
| --- | --- |
| MCP (26 tools) | Live queries mid-conversation |
| Hooks (6) | SessionStart brief, retrieval, re-index, compact continuity |
| Slash commands (15) | Operator controls for status, rebuild, goals, bans |
| Skills (3) | `/recall`, `/speak-like-operator`, `/total-recall:llm-setup` |
| Sources (10) | One index across Claude Code, OpenCode, Codex, Gemini, Cursor, Continue, Cline, Aider, Goose, Grok |

## Install

Marketplace (recommended):

```text
/plugin marketplace add 88plug/claude-code-plugins
/plugin install total-recall@88plug
```

### Grok Build

```text
grok plugin marketplace add 88plug/claude-code-plugins
grok plugin install total-recall@88plug --trust
```


Local checkout with [uv](https://docs.astral.sh/uv/) (development):

```bash
git clone https://github.com/88plug/total-recall.git
cd total-recall
uv sync
uv run total-recall --help
claude --plugin-dir "$PWD"
```

Or editable install with pip:

```bash
pip install -e ".[dev]"
claude --plugin-dir "$PWD"
```

!!! note
    Requirements are `bash` + `curl` + internet. The plugin bootstraps `uv`, Python,
    and deps into its own data dir on first hook fire. No system-wide Python required.

Per-client MCP wiring (OpenCode, Cursor, Gemini, …): see [Install overview](https://github.com/88plug/total-recall/blob/main/install/README.md).

## Quickstart

First run backfills transcripts in the background (detached; progress in
`logs/bootstrap.log`). Every new session then gets a short SessionStart brief for
the current directory.

```text
/recall-status
/recall what did we decide about the deploy pipeline?
```

Manual full reindex:

```bash
total-recall index --rebuild --jobs 4
# or, from a uv checkout:
uv run total-recall index --rebuild --jobs 4
```

Typical corpus: ~22s single-threaded → ~9s at `--jobs 4`.

## MCP tools (26)

The model calls these mid-conversation. Prefer the narrowest tool that fits.

**One-call SessionStart pattern:** `get_operator_context` — operator profile, voice,
active goal, recent corrections, and standing decisions in one payload.

### Core recall

| Tool | Use when |
| --- | --- |
| `recall` | Fuzzy topic lookup across extractions |
| `recall_targeted` | Before a default/recommendation — routes by intent |
| `prior_sessions_for_cwd` | Cheap session list for this directory |
| `get_session_digest` | Full structured digest of one session |
| `search_messages` | Exact phrase search in raw transcript lines |
| `find_failed_attempts` / `list_failed_attempts` | Past abandoned approaches |
| `find_user_preferences` | Stable prefs before suggesting a default |

### Operator-aware

| Tool | Use when |
| --- | --- |
| `get_operator_context` | Session start bundle (preferred) |
| `get_operator_profile` / `get_voice_profile` | Identity and register |
| `check_banned` | Pre-suggestion ban check |
| `get_active_goal` / `list_goals` | Goal stack for a cwd |
| `list_standing_decisions` / `get_decision_for_topic` | Durable choices |
| `recall_corrections_about` / `get_recent_corrections` | Past pushback |
| `get_past_truth_assertions` | Operator truth-assertion taxonomy |
| `assess_escalation_risk` | Pre-send risk check after friction |
| `get_project_graph` / `get_machine_inventory` / `define_term` | Ontology |

### Workflow and satisfaction

| Tool | Use when |
| --- | --- |
| `get_workflow_profile` | How the operator works (autonomy, fan-out, peak hours) |
| `get_satisfaction_profile` | Praise/frustration × assistant-turn shape |
| `list_implicit_preferences` | Behavior-derived prefs past promotion threshold |

Skill guidance for when to call what: `skills/recall/` (loaded as `/recall`).

## Slash commands (15)

| Command | Purpose |
| --- | --- |
| `/recall` | Query your memory |
| `/recall-status` | Index and ingest status |
| `/recall-inspect` | Inspect extracted records |
| `/recall-rebuild` | Full reindex |
| `/recall-promote` | Promote a signal to a standing decision |
| `/recall-operator-context` | Show bundled operator context |
| `/recall-corrections` | List corrections |
| `/recall-decisions` | List decisions |
| `/recall-goal` | Active goal |
| `/recall-check-banned` | Check banned actions |
| `/recall-escalation` | Escalation-risk assessment |
| `/recall-metrics` | Usage metrics summary |
| `/recall-cost` | Per-model token and cost breakdown |
| `/recall-topics` | Most-extracted topics |
| `/recall-health` | Ingest age, hook fire rate, latency, errors |

## Skills (3)

| Skill | Purpose |
| --- | --- |
| `/recall` | Orientation protocol for mining past sessions via MCP |
| `/speak-like-operator` | Voice-matching from `get_voice_profile()` |
| `/total-recall:llm-setup` | Manual fallback for local-LLM provisioning |

## What it captures

17 extractors: 11 per-session, 6 operator-level aggregators.

<details>
<summary>Per-session extractors (11)</summary>

- `corrections` — turns where you redirected the model
- `decisions` — "we're going with X because Y"
- `self_corrections` — model walked back its own claim
- `progress` — how far a line of work actually got
- `domain_facts` — durable codebase/environment signals
- `away_summaries` — recaps after returning to a stale session
- `model_corrections` — pushback paired with the rejected approach
- `standing_decisions` — durable across sessions
- `bans` — explicit "never do X"
- `goals` — what you said you are trying to achieve
- `truth_rhetoric` — objective-state assertions for later checking

</details>

<details>
<summary>Operator-level extractors (6)</summary>

- `operator_profile` — who you are, how you work across projects
- `voice_profile` — tone, phrasing, verbal tics
- `ontology` — project/machine/service vocabulary + co-mention graph
- `workflow` — fan-out, autonomy, interrupt rate, peak hours
- `implicit_preferences` — prefs expressed by behavior (multi-axis threshold)
- `satisfaction` — praise/frustration × prior assistant-turn shape

</details>

## Cross-CLI sources

One index spans **10** clients. Cross-source dedup keeps the highest-priority copy of
duplicated turns.

| Client | MCP | Hooks | Ingest |
| --- | --- | --- | --- |
| [Claude Code](https://github.com/88plug/total-recall/blob/main/install/claude_code.md) | yes | yes | yes |
| [OpenCode](https://github.com/88plug/total-recall/blob/main/install/opencode.md) | yes | no | yes |
| [Gemini CLI](https://github.com/88plug/total-recall/blob/main/install/gemini_cli.md) | yes | no | yes |
| [Codex CLI](https://github.com/88plug/total-recall/blob/main/install/codex.md) | yes | no | yes |
| [Cursor](https://github.com/88plug/total-recall/blob/main/install/cursor.md) | yes | no | yes |
| [Continue](https://github.com/88plug/total-recall/blob/main/install/continue.md) | yes | no | yes |
| [Cline](https://github.com/88plug/total-recall/blob/main/install/cline.md) | yes | no | yes |
| [Aider](https://github.com/88plug/total-recall/blob/main/install/aider.md) | no | no | yes |
| [Goose](https://github.com/88plug/total-recall/blob/main/install/goose.md) | yes | no | yes |
| [Grok](https://github.com/88plug/total-recall/blob/main/install/grok.md) | yes | no | yes |

```bash
total-recall sources list
total-recall sources detect
```

## Metrics

Local SQLite only — no external collector, no telemetry.

```bash
total-recall metrics summary [--since 7d] [--project PATH]
total-recall metrics cost [--since 30d]
total-recall metrics sessions [--top 10] [--by tokens|duration|corrections]
total-recall metrics topics [--limit 10]
total-recall metrics health
```

All subcommands support `--json`.

## Storage and privacy

Everything stays under `${CLAUDE_PLUGIN_DATA}/total-recall/` (env-resolved by Claude Code).
SQLite index (`index.db` + FTS5), optional embeddings, `state.json`, rotating logs.
Transcripts are never rewritten or re-uploaded.

!!! note
    Read-only on session JSONL. Dense embeddings use **product-owned ollama**
    (`qwen3-embedding:0.6b`). Same managed daemon as LLM refine (`qwen3.5:2b`).

## Optional local-LLM refinement

On first install, Total Recall can provision a small local model (`qwen3.5:2b` via
ollama) in the background. Refinement runs only on rebuild. Heuristics remain the
fallback if ollama is not ready.

See [Local-LLM refinement](https://github.com/88plug/total-recall/blob/main/llm-refinement.md) for env vars and troubleshooting.
Disable with `TOTAL_RECALL_LLM_PROVIDER=none`.

## Relation to amnesia

`amnesia` owns continuity **within** one session across compaction.
`total-recall` owns history **across** sessions and projects.
If both are installed, total-recall can read amnesia `memory/` snapshots as a
high-signal extra source without overwriting them.

## Next

| Page | Contents |
| --- | --- |
| [Architecture](https://github.com/88plug/total-recall/blob/main/architecture.md) | 4-layer pipeline (walker → extractors → index → delivery) |
| [Install overview](https://github.com/88plug/total-recall/blob/main/install/README.md) | Per-CLI MCP + ingest setup |
| [Marketplace](https://github.com/88plug/total-recall/blob/main/marketplace.md) | 88plug install path and plugin metadata |
| [CI/CD](https://github.com/88plug/total-recall/blob/main/ci.md) | Test matrix and release workflow |

## Contributing

```bash
uv sync --all-groups   # or: pip install -e ".[dev]"
uv run ruff check .
uv run pytest
uv run mkdocs build --strict
```

## License

[Functional Source License, Version 1.1, ALv2 Future License](https://github.com/88plug/total-recall/blob/main/LICENSE) (`FSL-1.1-ALv2`).

Free to use, copy, modify, and redistribute except Competing Use (offering this software
or a substantially similar substitute as a commercial product or service). Each release
converts to Apache 2.0 on its second anniversary. Commercial inquiries:
andrew@88plug.com.

## Features

| Area | What you get |
|---|---|
| Cross-session memory | Decisions, corrections, bans, goals, progress, domain facts from past sessions |
| Cross-CLI RAG | One index across 10 clients (Claude Code, OpenCode, Codex, Gemini CLI, Cursor, Continue, Cline, Aider, Goose, Grok) |
| MCP + hooks | 26 live tools, 6 hooks (SessionStart, retrieve, re-index, Pre/PostCompact continuity) |
| Operator discovery | Profile, voice, ontology, workflow, implicit prefs, satisfaction — data-driven from your transcripts |
| Local embeddings | SQLite FTS5 + ollama vectors; product-owned binary under plugin data dir |
| Metrics | Tokens, cost, topics, health — all from the local index, no telemetry |

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

Docs: [https://88plug.github.io/total-recall/](https://88plug.github.io/total-recall/). Architecture: [`docs/architecture.md`](https://github.com/88plug/total-recall/blob/main/docs/architecture.md).
