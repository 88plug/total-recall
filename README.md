> **Requirements:** `bash` + `curl` + internet. That's it. The plugin
> bootstraps everything else (`uv`, python, deps) into its own data dir
> on first hook fire. No system-wide `pip install`. No system python required.

# total-recall

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/88plug/total-recall)

Version: `v0.9.0` — see [git tag](https://github.com/88plug/total-recall/releases/tag/v0.9.0).

Cross-session memory for Claude Code. Mines your own `~/.claude/projects/<cwd-slug>/*.jsonl` session
transcripts and surfaces the useful parts — prior decisions, your corrections, approaches that
failed, real progress, plus a persistent profile of you (the operator) — to future Claude Code
sessions before the model has to ask.

The operator is the source of truth. Models change, projects come and go; the human running the
sessions is the one constant. v0.3 makes that explicit: an `OperatorProfile` and `VoiceProfile`
are first-class extracted artifacts, queryable in one MCP call at SessionStart.

## Why this exists

Claude Code today has three kinds of memory, and none of them mine the transcript history:

- **`amnesia`** (88plug) handles *intra-session* continuity across compaction. It snapshots the
  current working state so a single session survives a compact. It does not look at other sessions.
- **Auto-memory** (`~/.claude/projects/<proj-slug>/memory/`) is hand-curated by the user and the
  model. Useful, but it captures only what someone remembered to write down.
- **CLAUDE.md** is static, hand-edited, and global / per-project.

Meanwhile every session you've ever run is sitting on disk as append-only JSONL: every decision,
every "no, do it this way", every dead end. On a working machine that's tens of thousands of turns
across dozens of projects. Future sessions start blind to all of it.

`total-recall` reads that history (locally, read-only) and feeds the high-signal parts back to
new sessions in a low-token form.

## Quickstart

Local checkout (recommended during dev):

```bash
git clone https://github.com/88plug/total-recall.git
cd total-recall
pip install -e .[vec]
claude --plugin-dir "$PWD"
```

Marketplace install (once published):

```
/plugin marketplace add 88plug/total-recall
/plugin install total-recall@88plug
```

First run will auto-backfill your existing transcripts in the background (setsid-detached, so it
survives the spawning Claude Code session exiting; progress goes to `logs/bootstrap.log`).
Subsequent sessions get a SessionStart signpost summarizing what's relevant to the current `cwd`.

For a manual full reindex, `total-recall index --rebuild --jobs N` parallelizes ingest across
session files (a typical corpus drops from ~22s single-threaded to ~9s at `--jobs 4`).

## What it captures

17 extractors total. 11 run inline over each session's record stream; 6 are operator-level
aggregators that run out-of-band against the full corpus.

**Per-session (in pipeline):**

- `corrections` — turns where the user redirected the model ("no, not that way",
  `queue-operation` interrupts, restated requirements).
- `decisions` — "we're going with X because Y" moments, from assistant text and user-confirmed
  pivots.
- `self_corrections` — places the model corrected itself ("actually, scratch that") — useful
  signal that an earlier statement is now wrong.
- `progress` — how far a given line of work actually got. Anchors "we already did X".
- `domain_facts` — durable signals about the codebase / environment (versions, paths, conventions).
- `away_summaries` — recap text the user wrote after returning to a stale session.
- `model_corrections` — corrections specifically about model behavior / output format.
- `standing_decisions` — decisions the user explicitly marked as durable across sessions.
- `bans` — explicit "never do X" instructions.
- `goals` — what the user said they're trying to achieve in this session.
- `truth_rhetoric` — assertions the user made about objective state ("the deploy is broken",
  "X is the canonical file") — kept so a later session can check whether they still hold.

**Standalone (operator-level, run out-of-band against the corpus):**

- `operator_profile` — durable signals about *the human*: who they are, how they work,
  preferences across projects.
- `voice_profile` — how the user writes: tone, phrasing patterns, verbal tics. Lets a model
  match register without being told.
- `ontology` — vocabulary the user uses for their own systems (project names, machine names,
  service names) so a fresh session resolves jargon without asking. Also populates a
  cross-project co-mention graph (`projects.related_projects`) so the operator's portfolio
  shape — hub vs spoke projects, dependency direction — is queryable.
- `workflow` — *how* the operator works: fan-out vocabulary + per-session frequency, autonomy
  score, mid-flight interrupt rate, planning idiom, peak hours / preferred work window,
  session shape, subagent adoption rate. EMA-blended on the hot path.
- `implicit_preferences` — preferences the operator expresses by *behavior* rather than as
  a ban/decision: tool-call ratios (e.g. Edit vs Write), shell-command dominance within a
  group (e.g. uv vs pip), absence patterns, format preferences, recurring vocabulary. Promoted
  only when the signal crosses a multi-axis threshold (≥5 sessions, ≥3 projects, ≥7-day span,
  ≥80% non-contradiction).
- `satisfaction` — bidirectional praise/frustration profile paired with the preceding
  assistant-turn shape (`tool_call_brief`, `long_prose`, `confirmation_request`, etc.).
  Captures that for some operators satisfaction is silent — calibration on the absence of
  frustration, not just the presence of praise.

## Metrics

After your index is built, `total-recall metrics` gives you visibility into your own Claude Code usage — tokens spent, slowest sessions, most-corrected topics, compaction frequency — all from the local SQLite index. No external collector, no telemetry, no SaaS.

### Subcommands

- `total-recall metrics summary [--since 7d] [--project PATH]` — sessions, tokens (with cache-read %), wall vs active hours, estimated $ cost, top corrections, busiest project, longest session.
- `total-recall metrics cost [--rate model=in/out] [--since 30d]` — per-model token+cost breakdown using bundled default rates or your overrides.
- `total-recall metrics sessions [--top 10] [--by tokens|duration|corrections]` — rank sessions on a column.
- `total-recall metrics topics [--since 30d] [--limit 10]` — most-extracted topics across corrections and decisions.
- `total-recall metrics health` — last ingest age, hook fire rate, p95 latency, error count.

All subcommands support `--json` for piping into `jq` / spreadsheets.

### Example

```
$ total-recall metrics summary --since 7d
total-recall metrics — past 7d
  sessions: 42   projects: 6   compactions: 11
  tokens:  in 18.4M (62% cache-read), out 412k    ~ $14.20 @ sonnet
  active:  21.3h    wall: 38.1h
  top corrections: "use Edit not Write" ×6,  "no emojis in commits" ×4
  busiest project: /home/operator/acme-net  (8.1M tokens, 14 sessions)
  longest session: 73 min, 2.1M tokens — "feature-dev: relay-failover"
```

### Why not OpenTelemetry / Langfuse?

We considered both. Findings:

- **Langfuse** is wrong-shape: its core abstraction is `Generation` (LLM call), but total-recall doesn't call LLMs — it's a memory tool downstream of Claude Code. Plus self-hosting needs Postgres + ClickHouse + Redis (4GB RAM floor) for what amounts to a 50MB SQLite plugin.
- **OpenTelemetry** is deferred to v0.3. The official `modelcontextprotocol/python-sdk` issue #421 ("Add OTel") is still open and unmerged in mid-2026; manual span wrappers now would need rewriting when upstream lands. Claude Code itself already emits OTel at the host level (`CLAUDE_CODE_ENABLE_TELEMETRY=1`).
- **Native analytics over our own SQLite index** wins: every `usage{}` block, `compact_boundary`, and extracted correction is already queryable. `total-recall metrics` exposes that directly, with zero new dependencies and zero data leaving the machine.

When (a) the upstream MCP SDK lands OTel middleware, or (b) total-recall gets multi-user deployments where a central operator needs aggregated metrics, the path is wired-but-off: add an optional `[telemetry]` extra dependency group and emit spans alongside the existing metrics tables.

## Delivery surfaces

- **SessionStart signpost hook** — injects a short, budget-aware brief at session start. Default
  cap is a few hundred tokens; truthfully nothing if there's nothing useful to say. The
  recommended one-call pattern for the model is `get_operator_context`, which bundles the
  operator profile, voice profile, active goal, recent corrections, and standing decisions.
- **MCP server (26 tools)** — live queries the model can call mid-conversation.

  *v0.1 core (6):* `recall`, `prior_sessions_for_cwd`, `find_failed_attempts`,
  `find_user_preferences`, `get_session_digest`, `search_messages`.

  *v0.3 operator-aware (17):* `get_operator_context`, `get_operator_profile`,
  `get_voice_profile`, `recall_corrections_about`, `get_recent_corrections`,
  `list_standing_decisions`, `get_decision_for_topic`, `check_banned`, `list_failed_attempts`,
  `get_active_goal`, `list_goals`, `get_past_truth_assertions`, `assess_escalation_risk`,
  `get_project_graph`, `get_machine_inventory`, `define_term`, `recall_targeted`.

  *v0.8 workflow / satisfaction / implicit-prefs (3):* `get_workflow_profile`,
  `get_satisfaction_profile`, `list_implicit_preferences`.

- **`/recall` skill** — orientation-style guidance the model loads on demand for deeper dives.
- **`/speak-like-operator` skill** — operator voice-matching skill, runtime-populated from `get_voice_profile()`.
- **Slash commands** — for the human operator: `/recall`, `/recall-status`, `/recall-inspect`,
  `/recall-rebuild`, `/recall-promote`, `/recall-metrics`, `/recall-cost`, `/recall-topics`,
  `/recall-health`.

Each surface is independently disable-able. Convention-based discovery: the plugin manifest
declares no hook/skill/command/mcp keys — Claude Code picks them up from the sibling directories.

## Storage layout

Everything stays under `${CLAUDE_PLUGIN_DATA}/total-recall/` (env-resolved by Claude Code; do not
hardcode the path). Typical contents:

```
${CLAUDE_PLUGIN_DATA}/total-recall/
  index.db                       SQLite (FTS5 for keyword recall)
  vec.db                         sqlite-vec embeddings (only if [vec] extra installed)
  state.json                     last-indexed offsets per session file
  .bootstrapping                 lockfile present during first-run backfill
  .bootstrap_banner_shown        marker so the first-run banner is only shown once
  logs/
    hooks.log                    hook invocations + errors
    bootstrap.log                background backfill output (setsid-detached)
    events.jsonl                 NDJSON event stream (10MB rotation → events.jsonl.1.gz, .2.gz, …)
```

`index.db` schema by version:

- **v0.1:** `sessions`, `records`, `extractions` (+ FTS5 virtual tables).
- **v0.2:** adds `turns`, `compactions`, `ingest_runs` for per-turn and ingest-run accounting.
- **v0.3:** adds `operator_profile`, `voice_profile`, `standing_decisions`, `bans`,
  `failed_attempts`, `goal_stack`, `projects`, `machines`, `vocabulary`.
- **v0.5:** adds `source` + `dedup_superseded_by_source` columns for multi-CLI ingest —
  one index now spans Claude Code, OpenCode, Codex, Gemini CLI, Cursor, Continue, Cline, and
  Aider (cross-source dedup keeps the highest-priority copy of duplicated turns).

The session JSONLs themselves are never written to.

## Privacy

- **Read-only** on `~/.claude/projects/*.jsonl`. The Claude Code harness owns those files;
  `total-recall` only opens them with `O_RDONLY`.
- **Local-only.** No network calls. Embeddings (if enabled) run in-process via `fastembed`.
- **No re-uploading.** Transcripts contain secrets, internal URLs, and private code. They never
  leave the machine.

## Optional local-LLM refinement

By default, total-recall is 100% deterministic heuristics — zero model calls,
zero network egress. For operators who want sharper machine-name extraction,
vocabulary definitions, and per-project narratives, an OPT-IN refinement
layer can call a LOCAL LLM via ollama.

**Privacy stays intact**: transcripts never leave the machine. The cloud-API
path is deliberately not supported because it would break the no-reupload
guarantee.

Install ollama + pull the default model:

```bash
# ollama install: https://ollama.com
ollama pull qwen3.5:2b   # validated default: machines P/R 1.0, definition coverage ~0.60
```

Enable refinement (env vars; off by default):

```bash
export TOTAL_RECALL_LLM_PROVIDER=auto   # or 'ollama' to force; 'none' to disable
export TOTAL_RECALL_LLM_MODEL=qwen3.5:2b
export TOTAL_RECALL_LLM_BASE_URL=http://localhost:11434
total-recall rebuild --yes
```

Install the optional Python client conveniences alongside the `[vec]` extra:

```bash
pip install -e .[llm]
# or both at once:
pip install -e .[llm,vec]
```

Refinement runs ONLY during `rebuild` (cold path). If ollama isn't running
or the model isn't pulled, refinement is skipped silently and the
heuristic baseline remains authoritative.

| Env var | Default | Description |
|---|---|---|
| `TOTAL_RECALL_LLM_PROVIDER` | `auto` | `auto` probes the daemon; `ollama` forces; `none` disables. |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Model tag to use. Any ollama-pulled model works. |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://localhost:11434` | Ollama API endpoint. |

## Relation to amnesia

`amnesia` and `total-recall` are complements, not competitors:

| | amnesia | total-recall |
|--|--|--|
| Scope | One session, across compaction | Across sessions, across projects |
| Trigger | PostCompact / Stop hooks | SessionStart + on-demand MCP |
| Storage | Per-project `memory/` snapshots | Mined index from JSONL history |
| Owns | The *current* working state | The *historical* record |

If `amnesia` is installed, `total-recall` reads its `memory/` snapshots as a high-signal extra
source — it doesn't duplicate or overwrite them.

## Development

```bash
pip install -e .[dev,vec]
ruff check .
mypy total_recall
pytest
```

The architecture is a flat 4-layer pipeline; see [`docs/architecture.md`](docs/architecture.md).

## License

[Functional Source License, Version 1.1, ALv2 Future License](LICENSE.md)
(`FSL-1.1-ALv2`).

In plain English: free to use, copy, modify, and redistribute for any purpose
*except* a Competing Use — i.e. offering this software (or a substantially
similar substitute) as a commercial product or service. Each released version
automatically converts to the Apache License 2.0 on the second anniversary of
its release date.

For commercial-use inquiries that fall outside the Permitted Purpose:
claude@cryptoandcoffee.com.
