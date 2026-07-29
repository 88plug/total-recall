# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Current state: packaging 2.3.0 shipping** (rolling marketplace calver via hub; see `pyproject.toml` / `__version__`). Zero-config local-LLM (auto-provisioned ollama, `qwen3.5:2b` default) over the full on-disk corpus (claude_code + opencode cross-CLI). The pipeline (walker → extractors → SQLite/FTS5/vec index → hooks/MCP/skills/commands delivery) is wired end-to-end and shipping as a Claude Code plugin (live at `88plug/total-recall`). Trajectory:

- **v0.1** — 14 worktrees built the core pipeline (walker → extractors → SQLite FTS5/vec index → hooks/MCP/skills/commands). Docker validation found and fixed 16 issues (HIGH/MEDIUM/LOW).
- **v0.2** — metrics layer: `total-recall metrics summary|cost|sessions|topics|health` CLI, schema v2 tables (`turns`, `compactions`, `ingest_runs`, `schema_meta`), `events.jsonl` NDJSON stream, cost catalog.
- **v0.2.1** — first-run bootstrap (setsid-detached, `.bootstrapping` lockfile, one-shot banner); `--jobs N` parallel ingest (22s → 9s on real corpus); `turn_duration` → `turns.duration_ms` linking; MCP→events wiring.
- **v0.3** — 10 operator-aware extractors + 17 new MCP tools (in `mcp_server/extras/`) + `detector/` escalation heuristics + `skills/speak-like-operator/` + v2 SessionStart signpost that calls `get_operator_context` for a one-shot bundled load.
- **v0.7.x** — data-driven operator discovery retool (removed all hardcoded Andrew literals + `VOCABULARY_SEED`; tightened name detector with explicit-source precedence + product-noun filter; learned typos; IANA-anchored timezones; consolidation pass on rebuild). `tests/test_no_author_hardcoding.py` leak guard + `tests/test_generic_operator.py` synthetic-operator test + `tests/integration/test_backtest_real_corpus.py` real-corpus backtest.
- **v0.8.0** — three new aggregated profiles modeling **how** the operator works, not just who they are: `WorkflowProfile` (fan-out vocab, autonomy, interrupt rate, planning idiom, peak hours, session shape, subagent adoption), `ImplicitPreferenceProfile` (behavior-derived preferences with multi-axis promotion threshold), `SatisfactionProfile` (bidirectional praise/frustration × prior-assistant-turn-shape). Drift trigger added to `detector/escalation.py` (+2 risk). `extractors/ontology.py` populates the previously-empty `projects.related_projects` via a co-mention pass. 26 MCP tools total (added `get_workflow_profile`, `get_satisfaction_profile`, `list_implicit_preferences`).
- **v0.9.0** — optional `[llm]` local-LLM refinement layer (`extractors/llm/`). Off by default, ollama-only (cloud APIs deliberately excluded — would break the no-reupload-transcripts privacy guarantee), cold-path only. Refines machines NER, vocabulary definitions, project narratives. Env-driven (`TOTAL_RECALL_LLM_PROVIDER=auto|ollama|none`, `TOTAL_RECALL_LLM_MODEL` default `qwen3.5:2b`).
- **v0.9.1–v0.9.9** — iterative hardening of the `[llm]` layer: refinement fires + persists on rebuild, vocab-miner specificity, anti-echo + response cache + eval harness (`tests/integration/test_llm_eval.py`), and operator-profile mining across **all** CLI sources (not just Claude Code). Default model migrated `gemma4:e2b` → `qwen3.5:2b` (eval-won).
- **v0.10.0** — zero-config local-LLM: auto-provision ollama with no sudo, `qwen3.5:2b` default, text-generation refinement **on** by default.
- **v0.10.1** — 6-model bake-off (`qwen3.5:2b`/`4b`, `granite4.1:3b`, `nemotron-3-nano:4b`) confirms `qwen3.5:2b` as default (P/R 1.0, define-coverage 0.60, echo 0.14); shape-tolerant vocab/narrative parsing + fair model-bench harness. Stop/PostCompact incremental ingest detached (`recall::start_incremental_index`) so the watermark no longer stalls — the index now spans the full on-disk corpus (claude_code + opencode) instead of a ~270-session head.

The original greenfield framing below is preserved for historical context, but every shape it speculated about now exists.

### Operator-as-source-of-truth thesis (v0.3)

The human operator on a single-user machine is ground truth. Their corrections override training data. The model is a stateless surface that needs to be re-corrected every session unless we capture what was already settled. The v0.3 operator-aware extractors (decisions, corrections, bans, goals, voice, ontology, rhetoric) exist to make those corrections survive — so the next session arrives knowing what was already decided, what's banned, what the operator's voice sounds like, and which terms mean what *here*.

## What we're building

A Claude Code optimization layer that **data-mines the user's own session transcripts** at `~/.claude/projects/*/*.jsonl` so that future Claude Code sessions arrive with cross-session memory of: prior decisions, prior failures, what was tried, what worked, who the user is, and how far a given line of work has actually come. The user's framing: *"the AI has no idea how far we have come or how much valuable insight is just waiting there."*

Delivery shipped (and now coexist):

- **Plugin** (`.claude-plugin/plugin.json`) — distribution + slash commands + hooks.
- **MCP server** (`mcp_server/`) — **26 tools** registered (verify with `python -c "import mcp_server; print(len(mcp_server.mcp._tool_manager._tools))"`). Core (in `tools.py`): `recall`, `prior_sessions_for_cwd`, `find_failed_attempts`, `find_user_preferences`, `get_session_digest`, `search_messages`. Operator-aware extras (`mcp_server/extras/*.py`, one file per domain): `get_operator_context` (the SessionStart one-call bundler), `get_operator_profile`, `get_voice_profile`, `get_recent_corrections`, `recall_corrections_about`, `get_past_truth_assertions`, `list_standing_decisions`, `get_decision_for_topic`, `list_goals`, `get_active_goal`, `check_banned`, `list_failed_attempts`, `define_term`, `get_project_graph`, `get_machine_inventory`, `assess_escalation_risk`, `recall_targeted`.
- **Skills** — `skills/recall/` (general recall guidance) and `skills/speak-like-operator/` (operator voice-matching skill, runtime-populated from `get_voice_profile()`).
- **Background indexer** (`total_recall index`, hook-driven via Stop / PostCompact) — incremental SQLite/FTS5 (+ optional `sqlite-vec`) index of session JSONL.

When picking a surface, prefer the one that puts insight in front of the model **with the lowest token cost per session** — passive context injection beats forcing the agent to remember to query.

## On-disk data layout

Everything the plugin writes lives under `${CLAUDE_PLUGIN_DATA}/total-recall/` (the harness sets `$CLAUDE_PLUGIN_DATA`; locally it falls back to `~/.local/share/total-recall/`).

```
${CLAUDE_PLUGIN_DATA}/total-recall/
├── index.db            -- SQLite. v1: messages + extractions + FTS5 + ingest_state.
│                          v2 adds: turns, compactions, ingest_runs, schema_meta.
│                          v3 adds: operator_profile, voice_profile, standing_decisions,
│                          bans, failed_attempts, goal_stack, projects, machines, vocabulary.
│                          v4 adds: source + dedup_superseded_by_source columns (multi-CLI
│                          ingest: claude_code, opencode, codex, gemini_cli, cursor, continue,
│                          cline, aider — one index, cross-source dedup).
├── index.db-wal        -- WAL journal (TRUNCATE'd after every ingest)
├── index.db-shm        -- shared-memory file for WAL readers
├── .bootstrapping      -- lockfile present while first-run backfill runs (v0.2.1)
├── .bootstrap_banner_shown -- one-shot marker so the bootstrap banner only prints once
└── logs/
    ├── hooks.log       -- ring-buffered hook stderr (1 MiB cap, half-tail truncate)
    ├── bootstrap.log   -- background first-run backfill output (setsid-detached)
    └── events.jsonl    -- NDJSON event stream (rotates at 10 MiB → events.jsonl.N.gz)
```

The MCP server opens `index.db` in URI `mode=ro` so a crashing tool can never corrupt the index mid-conversation. Parent dir is created `0700` because transcripts are full of secrets / private URLs.

## Data source — what we're mining

Path pattern: `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`

- `<cwd-slug>` is the working dir with `/` → `-` (e.g. `-home-operator-my-project`).
- Today on this machine: ~19 projects, ~92 sessions, ~776 MB total. Individual sessions reach 14k+ lines.
- Each `.jsonl` is append-only. Lines are JSON, one record per line. No header.

### Record types observed (frequency-ordered in a real session)

| `type` | What it carries |
| --- | --- |
| `assistant` | Full assistant turn — `message.content[]` with `thinking`, `text`, `tool_use`, plus `model` and `id`. The dense signal. |
| `user` | User turn or tool results — `message.role` + `content`. |
| `queue-operation` | User input queued mid-turn (`operation: enqueue/...`, raw `content`). Captures interrupts/redirects. |
| `permission-mode` | Mode at session start (`bypassPermissions`, etc.). |
| `ai-title` | The auto-generated session title. **Use this as the cheap session-level label.** |
| `last-prompt` | Most recent user prompt + `leafUuid` pointer. Convenient resume marker. |
| `attachment` | Deferred-tool deltas, paste-cache refs, file attachments. |
| `system` | Subtypes like `turn_duration` (`durationMs`, `messageCount`) — useful for cost/perf mining. |
| `file-history-snapshot` | File-backup pointers for the file-history feature. |
| `agent-name` | Session-scoped label `{type, agentName, sessionId}` (no `uuid`/`parentUuid`/`timestamp`). Carried forward across sub-agent runs (e.g. `agentName: "general-purpose"`). Documented record type as of F9. |

### Cross-cutting keys (most records)

`sessionId`, `uuid`, `parentUuid` (forms a DAG, not a list — branches happen), `timestamp`, `cwd`, `gitBranch`, `version` (Claude Code version), `userType`, `entrypoint`, `isSidechain` (subagent turns), `requestId`, `promptId`, `slug`.

### Things to know before parsing

- **Not every line is a record** — blank lines occur. Skip them, don't error.
- Sessions are **DAGs via `parentUuid`**, not flat lists. Sidechains (`isSidechain: true`) are subagent runs and should usually be folded under their parent turn, not treated as peers.
- `assistant.message.content[].type === "thinking"` blocks contain the model's reasoning. High-signal for "why did we decide X" mining, but treat them as private — do not surface raw thinking text back to the user unless they've asked for it.
- `cwd` is the ground truth for "which project is this session about" — `<cwd-slug>` in the path can drift if directories get renamed.
- File sizes are large. Stream line-by-line; never `JSON.parse(entireFile)`.

## Related prior art already installed

- **`amnesia` plugin** (88plug) — solves *intra-session* continuity across compaction via PostCompact/Stop hooks and a per-project `memory/` dir. This project is the *inter-session* / *cross-project* complement; **don't duplicate amnesia's snapshot logic.** Read amnesia's plugin manifest at `~/.claude/plugins/cache/88plug/amnesia/*/` before designing handoff formats — its `memory/` layout is a precedent worth reusing where it fits.
- **`skill-creator` plugin** — reference for skill packaging if we ship a skill surface.
- **Auto-memory system** (per global `~/.claude/CLAUDE.md`) — file-based memory at `~/.claude/projects/<proj-slug>/memory/`. The data-mining output should *feed* this system, not bypass it.

## Design constraints

- **Read-only on session logs.** Never write into `~/.claude/projects/*/*.jsonl`. The Claude Code harness owns those files; mutation will corrupt active sessions.
- **No re-uploading transcripts.** Transcripts contain everything — secrets, internal URLs, private code. Any digest/index must stay local unless the user explicitly opts in to a remote step.
- **Streaming over slurping.** A single project can be hundreds of MB. Build for `for line in file:` from day one.
- **Cost-aware.** The whole point is to *save* the user context budget. A surface that injects 10k tokens of "helpful background" every session is a regression.

## Repo layout

Top-level directories (one per pipeline layer or delivery surface):

- `lib/` — JSONL walker, DAG resolver, sidechain folder, schema records.
- `extractors/` — pipeline-routed: `decisions`, `corrections`, `self_corrections`, `progress`, `domain_facts`, `away_summaries`, `model_corrections`, `standing_decisions`, `bans`, `goals`, `truth_rhetoric` + `secrets` (scrubber). Standalone (out-of-band): `operator_profile`, `voice_profile`, `ontology`, `workflow` (v0.8), `implicit_preferences` (v0.8), `satisfaction` (v0.8). Optional refinement: `extractors/llm/` (v0.9, ollama, opt-in).
- `index/` — SQLite/FTS5 store, `ingest.py` (incremental, inode-aware), `query.py`.
- `vec/` — `sqlite-vec` + **ollama embeds only** (`qwen3-embedding:0.6b`), RRF with FTS5.
- `mcp_server/` — MCPServer stdio server: core tools (`tools.py`) + resources.
- `mcp_server/extras/` — operator-aware tool surfaces, one file per domain. v0.3: `bans_tools.py`, `corrections_tools.py`, `decisions_tools.py`, `escalation_tools.py`, `goals_tools.py`, `ontology_tools.py`, `operator_context_tools.py`, `operator_tools.py`, `recall_targeted_tools.py`, `rhetoric_tools.py`, `voice_tools.py`. v0.8 additions: `workflow_tools.py`, `implicit_prefs_tools.py`, `satisfaction_tools.py`.
- `detector/` — escalation/risk heuristics (numeric scorer + state machine; backs `assess_escalation_risk`).
- `hooks/` — `SessionStart`, `UserPromptSubmit`, `Stop`, `PreCompact`, `PostCompact` bash entrypoints (`*.sh`; 6 scripts across 5 events — PostCompact runs both recovery + re-index) + Python query bridge (`lib/query.py`).
- `skills/`, `commands/` — slash-command and skill surfaces.
- `total_recall/` — CLI (`python -m total_recall ...`); includes `cmd_metrics.py` for the v0.2 metrics surface.
- `tests/` — unit tests + `tests/integration/` (corpus + golden-path).

## Validation

The codebase is validated by two coordinated harnesses:

- **Unit + integration tests** — `pytest tests/ -q` for unit (in-process synthetic corpus); `pytest tests/integration -q` for real-corpus & golden-path tests that read `~/.claude/projects/` and drive hooks/MCP end-to-end. Integration tests skip cleanly when the corpus is absent (CI containers).
- **Docker validation harness** — `Dockerfile.test` builds a Python 3.11-slim image with `jq`, `bash`, `sqlite3`, `mcp`, `click`, `sqlite-vec` pre-installed (dense embeds need host ollama). Integration golden path: `tests/integration/test_golden_path.py`.

**v0.9.0 surfaces to exercise during validation:** the 26 MCP tools (6 core in `mcp_server/tools.py` + 17 v0.3 operator-aware + 3 v0.8 behavioral, all in `mcp_server/extras/`); the `total-recall metrics {summary,cost,sessions,topics,health}` CLI; `get_operator_context` as the single SessionStart bundler the v2 signpost calls; the four new v0.8 consolidation passes on `rebuild` (workflow / implicit_preferences / satisfaction / ontology — populates `projects.related_projects` via the co-mention graph); and the optional v0.9 `[llm]` refinement (gated on `TOTAL_RECALL_LLM_PROVIDER` env + ollama daemon reachable + configured model pulled).
