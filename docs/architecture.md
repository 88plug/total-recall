# Architecture

`total-recall` is a 4-layer pipeline. Each layer has one responsibility, depends only on the
layer below it, and can be tested in isolation. The shape is deliberately boring: a walker feeds
extractors, extractors feed an index, the index feeds delivery surfaces.

```
                ~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl
                                    |
                                    v
            +-----------------------------------------------+
            |  (a) JSONL walker      lib/                   |
            |      stream lines, parse, resolve DAG         |
            +-----------------------------------------------+
                                    |
                                    v
            +------------------------+   +------------------+
            |  (b) Extractors        |   |  detector/       |
            |      extractors/       |-->|  escalation.py   |
            |   pipeline-routed (11) |   |  (consumes rows, |
            |   standalone     (6)   |   |   no rows out)   |
            |   + secrets scrubber   |   +------------------+
            +------------------------+
                                    |
                                    v
            +-----------------------------------------------+
            |  (c) Index             index/  vec/           |
            |      core FTS5  +  metrics sub-store          |
            |      +  operator sub-store (9 tables)         |
            |      +  sqlite-vec (optional)                 |
            +-----------------------------------------------+
                                    |
                                    v   <--- hooks/lib/query.py
                                    |        (hook<->index shim)
            +-----------------------------------------------+
            |  (d) Delivery     hooks/  mcp_server/         |
            |                   skills/ commands/           |
            |      SessionStart v2 signpost, 26 MCP tools,  |
            |      3 skills, 15 slash commands              |
            +-----------------------------------------------+
```

## Layer (a) — JSONL walker (`lib/`)

**Responsibility.** Turn a directory of append-only JSONL session files into a stream of typed
records, resolving `parentUuid` into a DAG and folding sidechains (`isSidechain: true`) under
their parent turn. Track per-file byte offsets in `state.json` so re-indexing is incremental.

**Dependencies.** Standard library only. No DB, no extractors. Pure parsing.

**Why it's its own layer.** Streaming + DAG resolution + offset bookkeeping is enough work that
mixing it into extractor code makes both harder to test. The walker has one job: yield clean
records, in DAG order, without OOMing on a 14k-line session.

## Layer (b) — Extractors (`extractors/`)

**Responsibility.** Walk the record stream emitted by layer (a) and emit structured facts. The
package ships 17 extractors + a universal scrubber, split into two execution lanes (plus an optional v0.9 LLM refinement lane).

**Pipeline-routed extractors** (run through `extractors/pipeline.py::ALL_EXTRACTORS`; each yields
`Extraction` rows that the ingest loop writes to `extractions`):

- `corrections.py` — user redirects (text + `queue-operation` interrupts).
- `decisions.py` — "going with X because Y" moments.
- `self_corrections.py` — model walks back its own previous claim within a session.
- `progress.py` — how far a given line of work actually got.
- `domain_facts.py` — durable cross-session signals (env, versions, conventions, identity).
- `away_summaries.py` — what changed while the operator was away (compaction-boundary deltas).
- `model_corrections.py` — pairs user pushback with the rejected approach (v0.3 highest-leverage signal).
- `standing_decisions.py` — durable "always X over Y" preferences.
- `bans.py` — provider/tool/pattern bans + failed attempts.
- `goals.py` — per-project goal stack with status state machine.
- `truth_rhetoric.py` — 7-category truth-assertion taxonomy.

**Standalone extractors** (run out-of-band from the main pipeline; each owns its own DB tables
and writes them directly via `index/`):

- `operator_profile.py` — identity/role aggregates → `operator_profile`.
- `voice_profile.py` — voice-fingerprint stats → `voice_profile`.
- `ontology.py` — project / machine / vocabulary graph → `projects` (incl. `related_projects` co-mention edges as of v0.8), `machines`, `vocabulary`.
- `workflow.py` (v0.8) — how the operator works: fan-out vocab, autonomy score, interrupt rate, planning idiom, peak hours, session shape, subagent adoption → `workflow_profile`.
- `implicit_preferences.py` (v0.8) — behavior-derived preferences (Edit vs Write, shell-command dominance, absence patterns, format prefs, recurring vocab) → `implicit_preferences`.
- `satisfaction.py` (v0.8) — bidirectional praise/frustration × prior-assistant-turn shape → `satisfaction_profile` (+ `satisfaction_meta`).

**Optional refinement lane** (v0.9+, `extractors/llm/`). Auto-provisioned on first install
when `TOTAL_RECALL_LLM_PROVIDER=auto` (default). Bootstrap can fetch a local ollama binary
and pull `qwen3.5:2b`. When a daemon is reachable and the model is pulled, `cmd_rebuild`
invokes refinement AFTER heuristic consolidation: machines NER filter, vocabulary
definitions, project narratives. Local-only via ollama (cloud APIs deliberately excluded —
would break the no-reupload-transcripts privacy guarantee). Heuristic baseline always wins
on disagreement; set `TOTAL_RECALL_LLM_PROVIDER=none` to disable.

**Universal scrubber.** `secrets.py` runs over every emitted row's `content` plus every string
field in `context` (recursive). It is invoked from the orchestrator, not the extractors, so a
new extractor cannot accidentally opt out.

**Dependencies.** Layer (a) only. Each extractor is independent — failing one does not affect the
others. Pipeline-routed extractors are pure functions from records to rows; standalone extractors
write to the index directly (the "pure" guarantee applies to the pipeline lane, not the package).

**Why it's its own layer.** Extractors are where the heuristics live, and heuristics churn.
Isolating them means new extractors are drop-in, and existing ones can be tuned without touching
the walker or the index.

## Layer (c) — Index (`index/`, `vec/`)

**Responsibility.** Persist extractor rows into a queryable store under
`~/.claude/plugins/data/total-recall/`.

- `index/` owns `index.db`: SQLite with FTS5 virtual tables for keyword recall plus relational
  tables for sessions, cwds, and extracted rows.
- `vec/` owns dense vectors in the shared index: `sqlite-vec` + **ollama embeddings by default** (format v2); `fastembed` is an explicit escape hatch only
  enabled only when the `[vec]` extra is installed. Vector recall is a query-time augmentation,
  not a replacement, for FTS5.

**Dependencies.** Layer (b) rows in, query API out. Knows nothing about hooks, MCP, or skills.

**Why it's its own layer.** Storage choices (FTS5 today, hybrid lexical+vector tomorrow,
something else later) are the most likely thing to change. Keeping all DB knowledge here means
the delivery layer is portable across index implementations.

## Layer (d) — Delivery (`hooks/`, `mcp_server/`, `skills/`, `commands/`)

**Responsibility.** Put recalled facts in front of the model with the lowest token cost per
session. Four surfaces, each optional:

- `hooks/` — `session-start-signpost.sh` (startup/clear brief),
  `session-start-compact-restore.sh` (post-compaction continuity),
  `user-prompt-retrieve.sh` (async mid-prompt augmentation),
  `pre-compact-seed.sh` (coding-continuity packet),
  `post-compact-recovery.sh` + `post-compact-index.sh` (restore + async reindex),
  and `stop-index.sh` (async reindex). Configured in `hooks/hooks.json`.
  All bash hooks share `hooks/lib/common.sh`; database reads route through `hooks/lib/query.py`,
  which is the only shim between hook code and the index.
- `mcp_server/` — 26 MCP tools total. **Core v0.1 (6, in `mcp_server/tools.py`):** `recall`,
  `prior_sessions_for_cwd`, `find_failed_attempts`, `find_user_preferences`,
  `get_session_digest`, `search_messages`. **v0.3 operator-aware (17, in
  `mcp_server/extras/*_tools.py`):** `recall_corrections_about`, `get_recent_corrections`,
  `list_standing_decisions`, `get_decision_for_topic`, `check_banned`, `list_failed_attempts`,
  `get_active_goal`, `list_goals`, `get_past_truth_assertions`, `get_project_graph`,
  `get_machine_inventory`, `define_term`, `get_operator_profile`, `get_voice_profile`,
  `get_operator_context`, `assess_escalation_risk`, `recall_targeted`. **v0.8 behavioral (3, also
  in `mcp_server/extras/*_tools.py`):** `get_workflow_profile`, `get_satisfaction_profile`,
  `list_implicit_preferences`.
- `skills/` — `recall/` (orientation guidance for the MCP surface),
  `speak-like-operator/` (voice-matching, runtime-populated from `get_voice_profile()`),
  and `llm-setup/` (manual fallback for local-LLM provisioning).
- `commands/` — 15 slash commands for the human operator: `/recall`, `/recall-status`,
  `/recall-inspect`, `/recall-rebuild`, `/recall-promote`, `/recall-metrics`, `/recall-cost`,
  `/recall-topics`, `/recall-health`, `/recall-check-banned`, `/recall-corrections`,
  `/recall-decisions`, `/recall-escalation`, `/recall-goal`, `/recall-operator-context`.

**Dependencies.** Layer (c) query API only. Surfaces never reach into the walker, extractors, or
raw JSONL — that keeps each surface trivially mockable in tests.

**Why it's its own layer.** Surfaces are the part the user actually feels, and they have very
different cost/latency characteristics (hook = every session, MCP = on demand, skill = explicit).
Separating them from the index lets us add/remove surfaces without touching the pipeline.

## Cross-cutting concerns

- **Read-only on transcripts.** Only layer (a) opens session JSONL files, always `O_RDONLY`.
- **Streaming everywhere.** No layer ever materializes a full session in memory.
- **Local-only.** No cloud APIs. LLM refinement and dense embeddings use the local
  ollama daemon on `localhost` (or `TOTAL_RECALL_LLM_BASE_URL`).
- **Convention-based discovery.** The plugin manifest declares no hook/skill/command/mcp keys;
  Claude Code discovers them from the sibling directories under the plugin root.

## Validation + observability roadmap

### Validation harness (current)

The pipeline is exercised in two complementary ways:

1. **In-tree pytest** — `tests/` covers each layer with unit tests against a
   synthetic corpus, and `tests/integration/` runs against the real
   `~/.claude/projects/` corpus (read-only). Integration tests skip cleanly
   on machines without a corpus so the same suite runs in CI containers and
   on the author's laptop.
2. **Docker validation harness** (`Dockerfile.test`) — a Python 3.11-slim
   image with `jq`, `bash`, `sqlite3`, `mcp`, `click`, `fastembed`, and
   `sqlite-vec` pre-installed. Agents mount the source at `/plugin` via
   `-v` and run the full test matrix inside the container, so we catch
   environment-shape bugs that pure-pytest misses (missing
   `$CLAUDE_PLUGIN_DATA`, missing `jq`, missing optional dependencies).
   A 10-agent validation pass against this harness produced the post-0.1.0
   bug-fix round (16 issues across HIGH/MEDIUM/LOW severity); regression
   tests for those fixes are pinned in `tests/integration/test_corpus.py`
   and `tests/integration/test_golden_path.py`.

### Observability roadmap

Decision: **native analytics over our own SQLite index.** Shipped as the v0.2 metrics layer
(see section below) — `turns`, `compactions`, `ingest_runs` tables populated during ingest,
queried via `total-recall metrics`. Zero new runtime dependencies, tightest fit for a
local-only tool whose value prop is "don't re-upload the user's transcripts."

- **OpenTelemetry SDK** — deferred to v0.3+ pending upstream MCP SDK PR #421 (still open as of
  mid-2026). Once merged, the OTel-shaped envelope already used by `recall::log_json` →
  `events.jsonl` becomes a drop-in upgrade path: same field names, real exporter behind a flag.
- **Langfuse** — rejected. Wrong abstraction: Langfuse models LLM-caller traces and
  total-recall is not an LLM caller. We index transcripts the user already produced; there
  is no prompt/response pair we own.

The ring-buffered hook log at `${CLAUDE_PLUGIN_DATA}/total-recall/logs/hooks.log` remains in
place for raw debug; structured per-event records go to `logs/events.jsonl` via
`recall::log_json` and are aggregated by `metrics health`.

## v0.2: metrics layer

The v0.2 milestone added a self-contained analytics surface over the SQLite index. Three new tables (`turns`, `compactions`, `ingest_runs`) are populated during ingest from `message.usage{}` blocks (assistant records) and `system.subtype=compact_boundary` payloads. The `total-recall metrics` CLI reads them.

Modules:
- `index/metrics.py` — pure aggregation functions (summary / cost / sessions / topics / health).
- `total_recall/cmd_metrics.py` — Click group with 5 sub-subcommands.
- `total_recall/cost.py` — model→$/Mtok catalog with cache-read multiplier; CLI override via `--rate sonnet=3/15`.
- `total_recall/events.py` — NDJSON event emitter with 10MB rotation, used by `metrics health` for hook fire-rate stats.

### Schema migration

`schema_meta.schema_version` goes from `'1'` to `'2'`. `db.py::apply_schema` is idempotent — every CREATE is `IF NOT EXISTS`. Existing v1 DBs auto-upgrade on next open.

## v0.3: operator-aware layer

### Thesis

The operator is the source of truth. Past sessions already encode the operator's standing
decisions, bans, goals, voice, and recurring corrections; instead of asking the model to
re-derive that context from raw history every session, v0.3 distills it into typed sub-stores
and ships it to the model as a structured 1.8 KB bundle at session start. The model gets
operator state up-front, not three corrections in.

### New extractors (10)

- `model_corrections.py` — pairs user pushback with the rejected approach (highest-leverage signal).
- `standing_decisions.py` — durable "always X over Y" preferences.
- `bans.py` — provider/tool/pattern bans + failed attempts.
- `goals.py` — per-project goal stack with status state machine.
- `truth_rhetoric.py` — 7-category truth-assertion taxonomy.
- `operator_profile.py` — identity / role aggregates (standalone, writes its own table).
- `voice_profile.py` — voice-fingerprint statistics (standalone).
- `ontology.py` — project / machine / vocabulary graph (standalone, writes three tables).
- `self_corrections.py` — model walks back its own claim within a session.
- `away_summaries.py` — what changed across compaction boundaries.

### New tables (9)

`operator_profile`, `voice_profile`, `standing_decisions`, `bans`, `failed_attempts`,
`goal_stack`, `projects`, `machines`, `vocabulary`. Each table is created by its owning module
in `index/` (e.g. `index/operator.py`, `index/bans.py`, `index/ontology.py`) using
`CREATE TABLE IF NOT EXISTS`, so existing DBs upgrade in place.

### New MCP tools (17) and the registration pattern

The 17 v0.3 MCP tools live in `mcp_server/extras/*_tools.py`. None of them are imported by the
tool implementations — `mcp_server/server.py` imports each extras module as a *side-effect
import*, and the `@mcp.tool()` decorators on the contained functions register them against the
shared `mcp` instance at boot time. This means adding a new tool surface is one decorator + one
line in `server.py`; there is no central registry to keep in sync. Each tool is independently
graceful: if its backing table is missing, it returns an error-marked result rather than raising,
so an incomplete index never breaks the rest of the surface.

### `detector/escalation.py` — sibling to extractors

`detector/` is a peer of `extractors/`, not a member of it. The distinction is that extractors
*produce* rows from records; the detector *consumes* rows (and live inputs from the current
turn) to score operator-frustration risk. `assess_escalation` returns a numeric
`ESCALATION_RISK` score, a 5-state classifier (`calm`, `mild_correction`, `escalated`,
`high_escalated`, `breaking_point`), and one of four `RecommendedAction` values
(`ship_as_is`, `trim_to_5_lines`, `run_command_paste_output`, `silence_then_act`). Exposed to
the model via the `assess_escalation_risk` MCP tool. Scoring weights and state thresholds are
spec-frozen by research note O9; do not tune them without updating the spec and the tests in
`tests/test_escalation.py`.

### SessionStart signpost: v1 → v2

v1 hand-rolled a "what's in memory" markdown block from a handful of queries. v2 makes a single
call to `get_operator_context(cwd)`, which returns a JSON-shaped payload (identity, active goal,
top standing decisions, top bans, voice cheat sheet, recent corrections, machines) capped at
~1800 chars. The bash hook (`hooks/session-start-signpost.sh`) emits it via Claude Code's
`additionalContext` channel, so the model sees it as part of its initial context rather than
as user input.

## Process model

### First-run bootstrap

A fresh install can't show recall context until the index has been built. The hook detects
fresh-install via `recall::is_fresh_install`, then `recall::start_bootstrap` kicks off
`total-recall index` in the background using `setsid` + `nohup` so the backfill survives the
hook's 5s timeout and the session ending. A `.bootstrapping` lockfile (PID + timestamp, stale
after 30 min) prevents duplicate bootstraps. A one-shot `.bootstrap_banner_shown` marker
ensures the user only ever sees the "total-recall: backfilling your sessions in the
background" banner once; the banner itself is produced by `recall::bootstrap_banner` and
emitted via the same `additionalContext` channel as normal signpost content.

### `--jobs N` parallel ingest

`total-recall index --jobs N` runs parse + extract across a `ProcessPoolExecutor`
(`index/ingest.py`); only the main process owns the SQLite writer, fed by `as_completed()` from
the pool. This sidesteps SQLite's single-writer constraint while still pinning all CPU-heavy
work to workers. On the author's real corpus this dropped a full reindex from ~22s to ~9s.
Default is 1 for incremental runs (overhead wins for small deltas) and `min(cpu_count, 8)` for full rebuilds.

### Hook fire matrix

| Hook | Trigger | Mode | Budget | Notes |
|---|---|---|---|---|
| `session-start-signpost.sh` | `SessionStart` (`matcher: "startup\|clear"`) | sync | 5s | Defers `compact\|resume` events to amnesia plugin. |
| `user-prompt-retrieve.sh` | `UserPromptSubmit` | async | 8s | Mid-prompt retrieval; never blocks the model. |
| `stop-index.sh` | `Stop` | async | 60s | Incremental reindex of the session that just ended. |
| `post-compact-index.sh` | `PostCompact` | async | 60s | Reindex after a compaction merges turns. |

### Event pipeline

Every hook emits structured records through the `recall::log_json <event> key=value …` bash
helper (`hooks/lib/common.sh`). Records land in `${CLAUDE_PLUGIN_DATA}/total-recall/logs/events.jsonl`
(10 MB rotation, see `total_recall/events.py`). `total-recall metrics health` aggregates them
into fire counts, p50/p95 latencies, and error rates per hook — the same surface that would
expose to OTel once PR #421 lands.
