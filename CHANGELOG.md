# Changelog

All notable changes to this project will be documented in this file.

## [0.13.1] - 2026-05-30

### Fixed — partial-schema CLI crashes + doc/flag drift (verify-sweep follow-up)

- HIGH: `stats`, `dump`, `inspect`, `metrics summary`, `metrics sessions` exited 2
  with an internal `no such table: messages` error when the index existed but had
  only the `adaptive`-created `reinjection_outcomes` table. `__main__.main()` now
  catches `sqlite3.OperationalError: no such table` centrally and emits a clean
  "index has not been built; run `index --full`" message with exit 1.
  Regression test: `tests/test_partial_schema.py` (4 commands).
- Slash commands `recall-status` / `recall-inspect` put the global `--json` flag
  after the subcommand (`No such option: --json`) — moved before it.
- `skills/llm-setup/SKILL.md` dead link `docs/install/llm-refinement.md` →
  `docs/llm-refinement.md`; stale `gemma4:e2b` default refs → `qwen3.5:2b`
  (SKILL.md + pyproject `[llm]` comment).
- Removed leftover executable `hooks/session-start-signpost-v1.sh.bak`.

Added `KNOWN_ISSUES.md` tracking the remaining verify-sweep defects.

## [0.13.0] - 2026-05-30

### Fixed — two critical `total-recall tail` crashes + version drift

A full-surface verification sweep (every CLI subcommand, MCP boot, hooks,
config plumbing) found defects the in-process unit suite never could, because
nothing exercised the cmd→index call sites or booted surfaces the way the
CLI/plugin harness does.

- `total-recall tail` crashed immediately: `cmd_tail` called
  `tail_loop(db_path=, cwd_filter=, ...)` but the helper takes
  `(conn, interval, projects_root, max_iterations)`. Now opens a connection and
  passes only accepted kwargs.
- `total-recall tail --once` crashed: the fallback passed `ingest_all(full=False)`
  but the kwarg is `force_full`; both branches had it so the `except` never
  helped. Fixed to `force_full=False`.
- `.mcp.json`: dropped a bad `TOTAL_RECALL_DB_DIR=${CLAUDE_PLUGIN_DATA:-./data}`
  default that pointed a dev checkout at a nonexistent `./data` and blocked the
  server's own `~/.local/share` fallback. `${CLAUDE_PLUGIN_ROOT:-.}` retained.
- `marketplace-entry.json` version corrected 0.9.0 → current (3-minor drift).

### Added — contract + consistency tests (guard the bug class)

- `tests/test_cli_contracts.py`: signature contract between `cmd_tail` and
  `index.tail.tail_loop` / `index.ingest.ingest_all`, all-13-subcommands
  `--help` smoke, and a bounded `tail_loop` run.
- `tests/test_version_consistency.py`: every version-bearing file
  (plugin.json, marketplace-entry.json, pyproject.toml, __init__.py) must agree.

## [0.12.0] - 2026-05-30

### Added — `total-recall sources verify`

A one-shot round-trip probe of every known CLI source (adapter load +
is_available() + session discovery count) in a single table. Fills the gap
between `sources list` (config/registration only) and `sources test <name>`
(one source): the single command to triage "why isn't source X showing up in
recall?" across all eight adapters at once.

- `total_recall/cmd_sources.py`: new `verify` subcommand; human table adds a
  `sessions` column, `--json` emits the full per-source round-trip shape.
- `tests/test_cmd_sources.py`: verify runs all known sources, JSON shape carries
  is_available/session_count/registered, empty-registry path still succeeds.

## [0.11.0] - 2026-05-30

### Fixed — LLM refinement no longer drops rows on truncated JSON

Full rebuilds with `--llm` occasionally logged `model response not valid JSON
— Unterminated string` and silently dropped the affected vocabulary/machine
row. Root cause: the model's output hit the `num_predict` ceiling mid-JSON.

- `extractors/llm/client.py`: raised default `num_predict` 512 → 1024 and added
  a truncation-aware retry — a JSON parse failure (the truncation signature)
  retries once at 2× the output budget before giving up, instead of returning
  None and dropping the row. Network/timeout errors still fail fast (no retry).
- The rebuild vocab upsert already preserves the prior DB definition when a
  refiner returns a falsy value, so a single bad call never erases a good row.

### Added — regression tests

- `tests/test_llm_client.py`: retry-on-truncation succeeds, retry is bounded
  (no infinite loop), and a valid first response issues no second call.
- `tests/test_incremental_flock.py`: drives the real `recall::start_incremental_index`
  bash helper — asserts a tick detaches a worker, and a second tick is skipped
  via `flock -n` while the lock is held. Locks the v0.10.1 watermark-stall fix.

## [0.10.1] - 2026-05-30

### Validated — 6-model bake-off confirms qwen3.5:2b as the default

Ran the eval harness across six local models (each with its OWN card-correct
sampling, not one profile forced on all). qwen3.5:2b wins — the default is
unchanged, now data-justified:

| model | machines P/R | echo_rate | define_coverage | machines latency |
|---|---|---|---|---|
| **qwen3.5:2b** (default) | 1.0 / 1.0 | **0.14** | **0.60** | 18s |
| qwen3.5:4b | 1.0 / 1.0 | 0.25 | 0.60 | 33s |
| nemotron-3-nano:4b | 1.0 / 1.0 | 0.17 | 0.40 | 22s |
| gemma4:e2b | 1.0 / 1.0 | weak | 0.20 | 82s |
| granite4.1:3b | 1.0 / 0.75 | 0.25 | 0.00 | 5.6s |
| qwen3.5:9b | 1.0 / 1.0 | 0.60 | 0.00 | 41s |

Bigger/newer did not win: nemotron 0.40 and granite 0.00 (+drops a real host)
both trail qwen3.5:2b's 0.60 coverage; granite is fastest but lowest quality.

### Added — nemotron sampling profile + machines fair-fight context

- `_SAMPLING_PROFILES["nemotron"]`: temp 0.6 / top_p 0.95 / top_k 20 + ollama
  `think:false` (nemotron is a reasoning model; verified the trace is cleanly
  suppressed — no `<think>` leak into JSON, unlike qwen3.5:9b). Activated for
  any `nemotron*` model via `TOTAL_RECALL_LLM_MODEL`. Granite/unknown models
  correctly fall to the greedy `default` profile (their cards specify no
  custom sampling, no reasoning mode).
- `refine_machines` now passes per-host context snippets so the classifier
  judges hostnames with real surrounding evidence (improves recall on bare
  single-word hosts).

Note on context windows: all candidates have 128K–262K native context, but
the refinement client pins `num_ctx=4096` deliberately — refinement inputs
are tiny (distilled tokens + ≤300-char snippets, never raw transcripts), and
a large KV cache only slows CPU inference. Native context is a non-factor in
the model choice for this task.

Full unit suite: 1186 passed.

## [0.10.0] - 2026-05-30

### Added — zero-config local-LLM: auto-provision + text-gen on by default

The optional LLM refinement now "just works" on a fresh install with no user
steps and no sudo.

- **Auto-provision (no sudo).** First-run bootstrap now fetches the ollama
  binary (~38 MB CPU build) into the plugin data dir — no system install, no
  root — starts a localhost daemon, and pulls the model, all in the
  background as a setsid-detached sidecar that can't slow or break the
  transcript backfill. New `recall::ollama` / `recall::ollama_serve` /
  `recall::ollama_pull` / `recall::provision_llm` / `recall::start_llm_provision`
  in `hooks/lib/common.sh`, mirroring the existing `uv` auto-install pattern.
  Every failure logs + returns 0 (LLM is optional, never fatal).
- **Default model qwen3.5:2b — and that's deliberately NOT resource-scaled.**
  A four-model eval (gemma4:e2b, qwen3.5 2b/4b/9b) on a real CPU box showed
  bigger is NOT better for this task: vocab define_coverage 2b=0.60, 4b=0.60,
  **9b=0.00** (9b leaks `<think>` despite `think:false`, producing all-null
  defs); echo_rate 2b=0.14 < 4b=0.25 < 9b=0.60. So **2b wins outright** and
  `autoselect_model()` always returns it (a downward floor only — never sizes
  up). `total-recall llm-model` is the single source of truth the bash
  provisioner calls (so it pulls exactly what Python requests — no drift);
  `TOTAL_RECALL_LLM_MODEL` overrides for advanced users who've tested another.
- **Model-family-aware sampling + think-off.** Qwen needs different sampling
  than gemma (temp 0.7 / top_k 20 / top_p 0.8 / presence_penalty 1.5 per the
  model card, NOT greedy) and `think:false` to stop `<think>` blocks breaking
  JSON. `_SAMPLING_PROFILES` + `_resolve_sampling(model)` apply the right
  family profile; fixed seed keeps temp>0 reproducible.
- **Text-gen refinement ON by default.** Vocabulary definitions + project
  narratives now run by default (was opt-in) — the eval shows clean synthesis
  on the default 2b (coverage 0.60 / echo 0.14), not the garbage-echo gemma
  produced. Opt out of text-gen with `TOTAL_RECALL_LLM_REFINE_TEXT=0`; opt out
  of the whole LLM layer with `TOTAL_RECALL_LLM_PROVIDER=none`. Machines
  refinement stays always-on. Cold-rebuild path only; cache-backed.
- **recall-cli DB-path fix.** A slash-command shell doesn't inherit
  `CLAUDE_PLUGIN_DATA`, so the CLI wrote the index to a phantom
  `~/.claude/plugins/data/total-recall/` instead of the harness path the MCP
  reads. The wrapper now derives `CLAUDE_PLUGIN_DATA` from its install path.
- **Docs + banner.** README "Optional local-LLM refinement" rewritten for the
  zero-config story (privacy-first: on-device, transcripts never leave the
  machine); new `docs/llm-refinement.md`; SessionStart banner announces the
  one-time background setup + the opt-out.

### Tests
+ model-autoselect, text-gen gate, ollama-provision (bash, hermetic — no
network/ollama). Full unit suite: 1185 passed, 12 skipped. Eval is the
authority on the model choice; numbers above are measured, not assumed.

## [0.9.9] - 2026-05-30

### Changed — default local model → qwen3.5:2b (won a live head-to-head eval)

Replaced the default refinement model gemma4:e2b with **qwen3.5:2b**, chosen
by running the eval harness against both on real fixtures (gemma4 deleted
from disk). Measured, not assumed:

| metric | gemma4:e2b | qwen3.5:2b | qwen3.5:4b |
|---|---|---|---|
| machines precision/recall | 1.0 / 1.0 | **1.0 / 1.0** | 1.0 / 1.0 |
| vocab define_coverage | 0.20 | **0.60** | 0.60 |
| vocab echo_rate | (weak) | **0.14** | 0.25 |
| machines latency | ~82s | **19s** | 33s |

qwen3.5:2b matches 4b on coverage with lower echo + faster machines, so it's
the default. Text-gen (definitions/narratives) is now genuinely usable (0.60
coverage, real synthesis not parroting) but stays opt-in via
`TOTAL_RECALL_LLM_REFINE_TEXT` for the per-rebuild latency; machines
refinement stays always-on.

### Fixed — model-family-aware sampling (qwen needs different settings than gemma)

The client previously hardcoded gemma-greedy options (`temperature=0`,
`top_k=1`) for every model. Qwen's official model cards explicitly recommend
against greedy decoding — it wants `temperature=0.7, top_k=20, top_p=0.8,
presence_penalty=1.5` (non-thinking/instruct, general text). Added
`_SAMPLING_PROFILES` + `_resolve_sampling(model)`: qwen models get the
card-recommended sampling, gemma/unknown stay greedy. A fixed `seed` keeps
runs reproducible despite `temperature>0`. Qwen3/3.5 also default to a
`<think>` reasoning mode that emits blocks which break JSON parsing — disabled
via the top-level `think: false` request field (ollama ≥ 0.9). Source: HF
Qwen3.5-2B / 3.5-4B model cards + ollama api.md.

### Fixed — recall-cli wrapper wrote the index to the wrong dir

`scripts/recall-cli.sh` runs from a slash-command shell that does not inherit
`CLAUDE_PLUGIN_DATA`, so `resolve_db_path()` fell back to
`~/.claude/plugins/data/total-recall/` — a different dir than the
harness-scoped `…/data/total-recall-88plug/total-recall/` the MCP server and
hooks read. A `rebuild` via the command wrote a phantom index the live plugin
never saw. The wrapper now derives `CLAUDE_PLUGIN_DATA` from its own install
path (`…/plugins/cache/<owner>/<name>/<ver>/` → `…/plugins/data/<name>-<owner>`)
when that data dir exists, so CLI + MCP + hooks all share one index. Dev /
source-repo runs (no `cache/` path) keep their existing fallback.

Full unit suite: 1163 passed. Live eval: qwen3.5:2b machines 1.0/1.0,
echo_rate 0.14, define_coverage 0.60.

## [0.9.8] - 2026-05-29

### Fixed — multi-source collector tags records with their origin

`lib/sources/collect.py` now sets `rec.source = <adapter name>` on each
yielded `Record` (best-effort). Previously the consolidation diagnostic
logged `multi-source collector: N records (unknown=N)` because the in-memory
`Record` carried no source attribute (source is tagged at ingest-persist
time, not on the dataclass). Purely a diagnostic accuracy fix — the records
were always genuinely multi-source (real rebuild: claude_code=133,911 +
opencode=1,526 messages mined into the profile); the log just couldn't
attribute them. Now the verbose line reports true per-source counts.

## [0.9.7] - 2026-05-29

### Added — operator profile now mined from ALL CLI sources, not just Claude Code

The thesis is "capture the operator's essence from every way they interact
with AI on the machine." Ingest was already multi-source (the `messages`
table is `source`-tagged across all 8 adapters), but the rebuild
**consolidation** — the pass that builds operator_profile / workflow /
implicit_preferences / satisfaction / ontology — globbed `~/.claude/projects`
only, so the profile was claude_code-only. On this machine that silently
dropped **1,526 OpenCode messages**.

- **`lib/sources/collect.py`** (new): `iter_all_source_records()` /
  `materialize_all_source_records()` — walks every available `SessionSource`
  adapter (`discover_sessions()` → `iter_records(session)`) and yields the
  common `Record` stream. One broken session/source logs a warning and is
  skipped, never aborting the pass.
- **Record-stream entry points** on the 5 consolidation extractors:
  `extract_operator_profile_from_records`, `extract_workflow_from_records`,
  `extract_satisfaction_from_records`, `extract_ontology_from_records`, and
  the implicit-preferences triples form. Each accepts the common `Record`
  (unwrapping via `.raw` / attribute access; satisfaction bridges `Record →`
  the raw-dict shape its DAG walk needs). Heuristic logic unchanged.
- **`cmd_rebuild`** materializes all-source records once and feeds all five.
  Ontology is dual-source: machines + vocabulary from the full multi-source
  stream, projects + co-mention graph still from the claude_code path glob
  (`persist_ontology` COALESCE-upserts so neither clobbers the other).
- **Scoping**: `--projects-root` overrides the *claude_code* projects dir
  only (it has no meaning for opencode/codex/…), so it now correctly **skips**
  the multi-source collector and scopes consolidation to that root. Without
  an override, the collector mines everything. (This bug — the collector
  ignoring `--projects-root` — was caught by `test_rebuild_consolidation`.)

### Added — multi-source backtest

`tests/integration/test_backtest_multi_source.py`: per-source tagging,
cross-source dedup, a **load-bearing regression** that a fact appearing ONLY
in a non-claude_code source reaches `operator_profile` via the from-records
path, and a real-machine test (ran ungated here: claude_code=133,581 +
opencode=1,526 messages). Existing consolidation/operator/ontology tests
updated to assert multi-source coverage.

Full unit suite: 1162 passed. Multi-source backtest: 5 passed (real data).

## [0.9.6] - 2026-05-29

### Fixed — real-usage reliability + guards so the bug classes can't recur

Two bugs that only bit when the plugin was actually used (not in isolated
unit tests), plus the guards that would have caught them.

- **Slash commands ran the CLI as bare `total-recall …`** — but that console
  script isn't on PATH for installed users (it lives in the plugin's uv
  venv), so the executing agent fell back to a system `python` (2.7 / 3.6 /
  one without deps) and `rebuild` failed outright. New `scripts/recall-cli.sh`
  wrapper resolves the interpreter exactly like the hooks (`recall::uv`:
  env override → PATH → ~/.local/bin/uv → bootstrapped uv → auto-install),
  then `uv run --project <root> python -m total_recall`. All 7 CLI-invoking
  commands (rebuild/cost/health/status/metrics/topics/inspect) now call the
  wrapper. Verified: `recall-cli.sh --version` → `total-recall, version`.

- **Workflow incremental crashed every Stop-hook ingest** —
  `extract_workflow_incremental` wanted `jsonl_paths` + a `WorkflowProfile`,
  but the ingest hot path passed records + the dict from `get_workflow()`
  (`'dict' object has no attribute 'sample_size'`). Swallowed by the
  try/except, so the workflow profile silently never updated incrementally.
  Refactored to a record-core: `extract_workflow_incremental(records,
  existing: WorkflowProfile|dict|None)` + a shared `_build_profile` /
  `_process_record`; `_normalize_existing` accepts the dict-with-sidecar
  shape. `persist_workflow` accepts dict|WorkflowProfile.

### Added — guard tests for both classes (the "ensure it can't recur" ask)

- `tests/integration/test_ingest_hotpath_clean.py`: runs the REAL ingest
  over a synthetic corpus and **fails on any swallowed "incremental update
  failed / consolidation skipped" warning**, and asserts the profile tables
  actually populated. This is the test that would have caught the workflow
  crash. Hermetic — no ollama, no network.
- `tests/test_command_invocations.py`: lints every command markdown — CLI
  invocations must use the `recall-cli.sh` wrapper, never bare
  `total-recall`/`python -m total_recall`. It immediately caught 3 stragglers
  (recall-cost/inspect/metrics follow-up suggestion lines) that the runner
  pass missed — now fixed.

Root cause both slipped CI: components were unit-tested in isolation with
correct types; nothing exercised the real ingest→profile wiring or the
command invocations, and the hot-path try/except suppressed the failure.

Full unit suite: 1149 passed.

## [0.9.5] - 2026-05-29

### Added — advanced LLM-refinement round (measured against live gemma4:e2b)

An expert round driven by a new reproducible eval harness. Headline: the
anti-echo work eliminates the v0.9.4 generation failure, and an eval harness
makes future model/prompt changes measurable instead of vibes.

- **Anti-echo detector** (`extractors/llm/refine_ontology.py`): `_is_echo()`
  rejects an LLM definition/narrative that parrots its grounding snippet
  (token-containment ≥ 0.75 OR verbatim substring) → returns null instead.
  Combined with **rewritten few-shot prompts** ("restate in your own words; do
  not copy; return null if you can't"), measured **echo_rate dropped to 0.000**
  on e2b (was ~everything echoing). Output is now clean definitions or null —
  never garbage.

- **Client-owned response cache** (`extractors/llm/client.py`): `LLMClient`
  now holds an `LLMCache` (built in `get_default_client()` at
  `$CLAUDE_PLUGIN_DATA/total-recall/llm_cache.db`); `generate_json` checks it
  before the HTTP call and stores after. Identical LLM calls across rebuilds
  are now instant — no repeat inference. Non-fatal if the cache can't open.

- **Machines refinement hardened** (`extractors/llm/refine_machines.py` +
  `total_recall/cmd_rebuild.py`): `isinstance` guard on the model's keep-list
  (no crash/pollution from non-string elements); a bare-single-word-hostname
  rescue rule in the prompt (keeps `novabox`-style hosts when context shows
  machine usage); and `cmd_rebuild` now gathers per-host FTS context snippets
  and passes them as `sample_contexts`. Measured machines **precision/recall
  1.000 / 1.000**, deterministic keep-set.

- **Eval harness** (`tests/integration/test_llm_eval.py` +
  `tests/local/llm_eval_fixtures.py`): gated on `TOTAL_RECALL_LLM_EVAL=1` +
  ollama reachable; runs per-model (`TOTAL_RECALL_LLM_EVAL_MODEL`). Reports
  machines P/R + determinism (hard asserts) and vocab echo_rate /
  define_coverage / latency (scorecard). Skips cleanly without the gate.

### Changed — text-gen gating rationale

`TOTAL_RECALL_LLM_REFINE_TEXT` stays opt-in (default off), but the reason is
no longer "echoes garbage" (fixed) — it's now "clean but low-yield on small
models" (e2b define_coverage ~20%; a ≥7B model raises it). Machines
refinement (classification) remains always-on when a model is available.

Full unit suite: 1130 passed. Live e2b eval: machines 1.0/1.0, echo_rate 0.0.

## [0.9.4] - 2026-05-28

### Fixed — vocab harness-artifact filter + gate generation-based LLM refinement

A real-corpus rebuild + a standalone LLM diagnosis (default model `gemma4:e2b`)
surfaced two things:

- **Vocab admitted harness/tooling artifacts** via the v0.9.3 hyphen
  specificity marker: `task-notification`, `task-id`, `tool-use-id`,
  `output-file`, `claude-1000` (tmp dir), `rw-rw-r` (ls output),
  `home-andrew-ip-service-for-docker` (cwd-slug), `toolu_*` (tool-call IDs).
  These are Claude Code session-mechanics noise, never operator vocabulary.
  Added `_is_harness_artifact()` to the miner: exact-token blocklist +
  pattern drops (`toolu_*`, `claude-<digits>`, leading `home-` slug segments,
  unix permission strings, size tokens). `ip-service-for-docker` (real
  project) survives; `home-andrew-ip-service-for-docker` (slug) drops.

- **The default CPU model echoes on generation.** Standalone diagnosis showed
  `gemma4:e2b` (2.3B) parrots the grounding snippets back instead of
  synthesizing a definition (e.g. `wireguard` → "Agent W: Live WireGuard
  tunnel… | Task #99…"). Classification works (machines keep/drop 150→10);
  generation (definitions/narratives) does not on a 2B model. So vocab-def +
  project-narrative refinement is now gated behind `TOTAL_RECALL_LLM_REFINE_TEXT`
  (default OFF) — it wants a capable model (>=7B). The machines refinement
  (classification) stays always-on when the client is available. Default
  rebuilds no longer burn ~140s producing echo-garbage.

New test: `test_mine_vocabulary_drops_harness_artifacts`. Full suite: 1107 passed.

## [0.9.3] - 2026-05-28

### Fixed — vocabulary miner specificity (lifts both heuristic + LLM-refined vocab)

The data-driven `mine_vocabulary` surfaced generic single English words by raw
frequency (`andrew`, `home`, `name`, `state`, `agent`, `read`, `ctrl`),
polluting the `vocabulary` table and feeding the LLM refinement garbage inputs
(which it correctly nulled — wasted work). Added a specificity gate in
`extractors/ontology.py`: a token is promoted only if it carries a structural
marker (hyphen / digit / dot / multi-word / internal-caps) OR is a bare alpha
word that is NOT in an English wordlist AND ≥ a minimum length. This keeps
coined/domain names (`opnsense`, `wireguard`, `litestream`, `relay-eu-west`,
`racknerd-4b4fa33`) and drops common English. Real-corpus top-40 after the fix
is all domain-specific (`ip-service-for-docker`, `opnsense`, `wireguard`,
`relay-us-east`, `docker-compose`, …) with zero generics.

### Fixed — operator-specific literal in co-mention stopwords

`extractors/ontology.py` `_CO_MENTION_STOPWORDS` hardcoded the author's
username `andrew` (a v0.8.0 leftover, despite a "no operator-specific
literals" comment) — it only stripped one operator's username and did nothing
for anyone else. Removed; the co-mention path relies on the generic
short-name / stopword filters. Neutralised remaining illustrative comments
that used the real first name (`andrew@example.com` → `dana@example.com`) and
fixed a stale `speak-like-andrew` → `speak-like-operator` skill reference.

New ontology tests: `test_mine_vocabulary_drops_generic_english`,
`test_mine_vocabulary_keeps_operator_specific`. Full unit suite: 1106 passed.

## [0.9.2] - 2026-05-28

### Fixed — LLM refinement now fires + persists during rebuild (validated end-to-end on real corpus + live ollama)

Two bugs found by running `rebuild` against a live ollama daemon + the
`gemma4:e2b` model on the operator's real corpus (not synthetic):

- **Availability probe lost a race under load → refinement silently skipped.**
  `LLMClient`'s `_probe()` used a 2s timeout, single attempt, result cached.
  During `rebuild` the probe fires immediately after CPU-heavy ingest +
  consolidation, so the 2s GET `/api/tags` timed out → cached `available=False`
  → the whole refinement layer was skipped for the run (verbose:
  `[rebuild] LLM refinement off`). The same client probed `available=True`
  when the box was idle. Fix: probe timeout 2s → **10s**, plus **3 retries
  with 1.5s backoff** before caching False. Rebuild now logs
  `[rebuild] LLM refinement enabled` and the machines refinement runs.
  Verified: persisted `operator_profile.machines` went 150 → **10 real
  hosts** through the actual plugin command.

- **`upsert_vocabulary_term` called with positional kwargs → ontology
  refinement raised + was skipped.** `category` / `frequency` are
  keyword-only (after `*`) in the real signature; `cmd_rebuild` passed them
  positionally (`takes 3 positional arguments but 5 were given`). Fixed to
  keyword args. Vocabulary + project-narrative refinement now completes
  without raising.

### Known limitation — vocabulary/narrative quality is bounded by the heuristic miner

With refinement firing correctly, vocab + narrative output is still weak on
the current corpus: the upstream `mine_vocabulary` heuristic surfaces generic
single-word tokens (`andrew`, `home`, `name`, `state`) by raw frequency, and
their grounding snippets are command-output noise (`du -h` fragments). The
LLM correctly returns null rather than hallucinate a definition from garbage,
so those terms keep their heuristic stubs. The machines refinement is the
proven win; vocab/narrative needs an extractor-level specificity filter on
the miner (drop common-English single words, require cross-project
specificity) — tracked as the next follow-up. The LLM layer itself is
working as designed (no crash, no hallucination).

## [0.9.1] - 2026-05-28

### Added — LLM refinement: CPU-tuned defaults + auto-setup UX (validated end-to-end)

Concrete tuning + first-run experience for the v0.9.0 optional refinement
layer. Verified against the live ollama Go source (`api/types.go
DefaultOptions`) and the official `gemma4:e2b` library page.

- `extractors/llm/client.py`: `generate_json` now passes a full CPU-optimised
  options block on every call: `temperature=0`, `top_k=1`, `top_p=1.0`,
  `seed=42`, `repeat_penalty=1.0` (disabled — interacts badly with repeated
  JSON keys at temp=0), `num_ctx=4096` (vs ollama's default of 0 which
  negotiates the model's 128K training max and allocates a huge KV cache on
  CPU first-load), `num_predict=512` (hard output ceiling). Plus
  `keep_alive="15m"` so the model stays resident across the rebuild's many
  sequential calls and auto-evicts after.
- `gemma4:e2b` verified on `ollama.com/library/gemma4`: 2.3B effective /
  5.1B total parameters, 128K context, q4_K_M quantization (~7.2 GB on
  disk), JSON/structured-output supported, designed for CPU inference.
- `hooks/session-start-signpost.sh`: one-time SessionStart detection of
  missing ollama / unreachable daemon / un-pulled model. Appends a clear
  one-line notice to the context payload (or skips silently if
  `TOTAL_RECALL_LLM_PROVIDER=none`). Suppressed after first display via
  `${RECALL_DATA_ROOT}/.ollama_notice_shown` sentinel.
- `skills/llm-setup/SKILL.md` + `scripts/llm-setup.sh`: new
  `/total-recall:llm-setup` skill that installs ollama (if missing, via
  the official installer — needs sudo), starts the daemon, pulls the
  configured model, and runs a smoke test against the refinement client.
  Idempotent.

UX shape per Claude Code plugin reality: there is no `install` lifecycle
hook, so first-run detection lives on `SessionStart` + an operator-invoked
`/total-recall:llm-setup` skill handles the actual install with explicit
consent. No silent ~7 GB downloads.

### Fixed — discovered by real-corpus validation

End-to-end run against a live ollama daemon + gemma4:e2b on this machine
surfaced two real wiring gaps:

- **Default timeout 60s was too short for CPU cold-load** (first call always
  timed out; model load alone is multiple seconds before any inference).
  Bumped to **180s**.
- **`cmd_rebuild` was calling `refine_vocabulary_definitions` and
  `refine_project_narratives` with no context snippets**, so the
  anti-hallucination guard correctly collapsed every output to null. Now
  enriches each call: vocab gets the top-3 user-turn FTS hits per term
  (rejecting tab/newline-heavy raw command output that misleads the model);
  projects get the top-3 short user turns from the cwd's own sessions.
  Calls run on top-50 vocab / top-25 projects by frequency, not blind
  alphabetical first-N.

### Validated against real corpus
- `refine_machines`: live run on this machine reduced a 150-entry
  heuristic-noise machines dict to **11 real hosts** in ~82s. All kept
  entries genuinely look like hostnames (`fly.io`, `mail.acme-net`,
  `relay-eu-west`, `racknerd-4b4fa33`, …); all dropped entries were
  English nouns / command fragments (`accessible`, `activity`,
  `brute-force`, `commands`, …). The NER-hard noise problem documented
  as a v0.8.0 known limitation is now fixed when the refinement layer
  is enabled.
- `refine_vocabulary_definitions` + `refine_project_narratives`:
  wire green; output quality is bounded by the upstream heuristic miner,
  which currently surfaces too many generic single-word tokens
  (`andrew`/`home`/`name`/`state`) by frequency alone. The LLM cannot
  rescue a bad input; an extractor-level filter (cross-project
  specificity score, hyphen/digit/length-6+ requirement) is the right
  follow-up. Tracked as a known limitation.

## [0.9.0] - 2026-05-28

### Added — optional local-LLM refinement (off by default)

New `[llm]` extra wires an opt-in refinement layer for the operator profile.
Refines machines (filters NER-hard noise from the heuristic dict),
vocabulary (per-term definitions from the operator's own corpus context),
and project narratives. Local-only via ollama; transcripts never leave the
machine. Cloud APIs (Anthropic / OpenAI / etc.) deliberately not supported
— sending transcripts off-machine would break the privacy guarantee.

Env vars: `TOTAL_RECALL_LLM_PROVIDER` (default `auto`), `TOTAL_RECALL_LLM_MODEL`
(default `gemma4:e2b`), `TOTAL_RECALL_LLM_BASE_URL`. Graceful absence:
skipped silently if ollama/model not present, heuristic baseline stays
authoritative.

## [0.8.0] - 2026-05-28

### Added — modeling HOW the operator works, not just WHO they are

Three new aggregated profiles, all data-driven, no LLM, no operator-specific
hardcoding. Each ships an extractor, persistence, an MCP tool, and a
consolidation pass on `rebuild`. Total MCP tools: 23 → 26.

- **`WorkflowProfile`** (`extractors/workflow.py`, `index/workflow.py`, MCP
  `get_workflow_profile`): captures how the operator works — fan-out
  vocabulary + per-session frequency, autonomy score (ratio of short
  execute-style turns to question turns), mid-flight interrupt rate,
  detected planning idiom (waves/phases/steps/sprints), peak hours and
  preferred work window, session shape (ops_burst / focused / marathon /
  bimodal / mixed), subagent adoption rate. EMA-blended on the hot path.

- **`ImplicitPreferenceProfile`** (`extractors/implicit_preferences.py`,
  `index/implicit_preferences.py`, MCP `list_implicit_preferences`): captures
  preferences the operator expresses by behavior rather than by ban/decision
  — tool-call ratios (e.g. Edit vs Write), shell-command dominance within
  functional groups (e.g. uv vs pip), absence patterns, format preferences
  (e.g. emoji-free), recurring vocabulary phrases. Promoted only when the
  signal crosses a multi-axis threshold (≥5 sessions, ≥3 projects, ≥7-day
  span, ≥80% non-contradiction).

- **`SatisfactionProfile`** (`extractors/satisfaction.py`,
  `index/satisfaction.py`, MCP `get_satisfaction_profile`): a bidirectional
  praise/frustration model paired with the preceding assistant-turn shape
  (`tool_call_brief`, `long_prose`, `confirmation_request`, etc.). Captures
  that for some operators satisfaction is silent — calibration must work on
  the absence of frustration, not just the presence of praise.

### Added — drift trigger in escalation detector

`detector/escalation.py` gains a `drift` trigger (+2 risk) matching
"drifting / off track / diverging" patterns. Existing trigger weights and
state thresholds unchanged.

### Added — cross-project co-mention graph

`extractors/ontology.py` now populates the previously-empty
`projects.related_projects` JSON column via a co-mention pass over each
project's session text. Surfaces the operator's portfolio shape (hub vs
spoke projects, dependency direction) for any operator with multiple cwds.

### Coverage

Adds 103 new unit tests (workflow 30, implicit_preferences 21, satisfaction
20 + escalation 25 still green, ontology +1 co-mention). Full unit suite:
1038 passed.

## [0.7.3] - 2026-05-27

### Fixed — data-driven operator discovery quality

A real-corpus backtest surfaced two discovery bugs in the per-file
incremental ingest path (the synthetic test only exercised the full-pass
extractor):

- **Timezone** could emit a non-zone fragment (any capitalized `Word/Word`
  matched the IANA pattern). Now anchored to real region prefixes and
  guarded at reduce time — invalid candidates yield an empty timezone
  rather than garbage.
- **Handle** could rank a frequently-mentioned project over the operator's
  real handle. Candidates corroborated by the resolved email local-part /
  domain / name now get a frequency boost so the operator's own handle
  wins. Derived at runtime; no hardcoded identity.

**Consolidation pass on rebuild.** The profile update runs per-file during
ingest, and append-supersede can freeze an early, non-global winner for
frequency-ranked identity scalars. `total-recall rebuild` now re-derives
the operator profile in a single full-corpus pass after ingest and persists
the globally-correct values — the cold-path reconcile the incremental hot
path defers to.

New tests: timezone-garbage rejection, handle email-reinforcement,
full-vs-incremental agreement guard, and backtest assertions that exercise
the incremental + persisted path against a real corpus.

## [0.7.2] - 2026-05-27

### Reverted

Reverts v0.7.1 (commits `ca2d0f9` and `5c1c052`). The duplicate `total-recall`
MCP entry that v0.7.1 set out to fix was a **local-only artifact**: it only
manifested when the working directory happened to be the plugin source repo,
because Claude Code's cwd-side `.mcp.json` discovery and the marketplace-
installed plugin's bundled `.mcp.json` both registered the same server. End
users installing via the marketplace never saw the duplicate.

v0.7.1's "fix" — ripping `.mcp.json` out of the public plugin and inlining
`mcpServers` into `plugin.json` — was therefore a public-surface refactor for
a one-machine cosmetic dupe. This release restores the prior layout:

- `.mcp.json` and `.mcp.json.README.md` restored at repo root.
- `mcpServers` block removed from `.claude-plugin/plugin.json`.
- `MANIFEST.in`, `docs/marketplace.md`, `docs/ci.md` restored to pre-v0.7.1.

The local collision should be resolved per-machine by removing the cwd-side
`.mcp.json` when working inside the plugin source repo, not by churning the
shipped plugin.

The v0.7.1 tag remains on `origin` (immutable history); anyone who installed
v0.7.1 will roll forward to v0.7.2 via the marketplace.

## [Unreleased]

### Documentation — data-dir path convention

The active data dir for v0.7.x is:

```
$CLAUDE_PLUGIN_DATA/total-recall/
```

When `CLAUDE_PLUGIN_DATA` is set by the Claude Code harness, it is already
plugin-scoped (`~/.claude/plugins/data/total-recall-88plug/`), so the
appended `total-recall/` subdir creates a cosmetic double-nest
(`…/total-recall-88plug/total-recall/index.db`). This is harmless — every
read and write uses the same resolver — but visually odd. A clean fix
(use bare `CLAUDE_PLUGIN_DATA` when set, append `total-recall/` only on
the standalone-script fallback path `~/.claude/plugins/data/`) is deferred
to v0.8.0 with an in-place migration so existing indexes are preserved.

### Stale-dir cleanup (claude-recall → total-recall rename)

Earlier development used the name `claude-recall`. After the rename to
`total-recall`, the following dirs may persist on machines that ran both
generations. They are safe to delete — nothing reads them on v0.7.x:

- `~/.local/share/claude-recall/`
- `~/.claude/plugins/data/claude-recall/`
- `~/.local/share/total-recall/` (empty placeholder created during the
  switchover before `CLAUDE_PLUGIN_DATA` took precedence)

```bash
rm -rf ~/.local/share/claude-recall \
       ~/.claude/plugins/data/claude-recall \
       ~/.local/share/total-recall
```

The active index lives at `$CLAUDE_PLUGIN_DATA/total-recall/index.db` and
is unaffected.

## [0.7.1] - 2026-05-27 — reverted by 0.7.2

Moved MCP registration inline into `.claude-plugin/plugin.json`'s `mcpServers`
block and removed `.mcp.json` / `.mcp.json.README.md`, to fix a duplicate
`total-recall` MCP entry seen in `/plugin`. The duplicate turned out to be a
**local-only artifact** (cwd-side `.mcp.json` discovery colliding with the
marketplace-bundled copy, only when working inside the plugin source repo),
so this public-surface change was unnecessary. **Fully reverted in 0.7.2** —
see that entry. Tag remains on `origin` as immutable history.

## [0.7.0] - 2026-05-27

### Changed
- **Eliminated the host-python prereq.** v0.6.2 still required `python3.10+`
  on `PATH` (or under `~/.local/share/uv/python/`). Boxes that ship `python3
  → 3.6` (Ubuntu 18.04, RHEL/CentOS 7-derivatives) had no path to install the
  plugin without first installing a newer python — which is exactly what we
  don't want users to do for a Claude Code plugin.
- Hooks now invoke `total_recall` via **uv** (`uv run --project ... python -m
  total_recall ...`). uv brings its own python (downloads + manages it
  transparently), so the host only needs `bash`, `curl`, and internet.
- `recall::uv` resolver added to `hooks/lib/common.sh`. Order:
  1. `$RECALL_UV` env override
  2. `uv` on `PATH`
  3. `$PLUGIN_DATA/bin/uv` (bootstrapped on a prior fire)
  4. **download `uv` via the official static installer** into
     `$PLUGIN_DATA/bin/uv` (one-time, ~5s for the binary; first `uv run`
     then takes ~30s to download python 3.12; subsequent runs are <1s
     because uv caches the venv in `$PLUGIN_ROOT/.venv`).
- `start_bootstrap` rewritten to use `$uv run` instead of `"$py" -m`.

### Added
- `recall::run` — convenience wrapper (`recall::run -m total_recall index ...`).
- `recall::require_uv` — fail-fast variant for hook entry points.

### Compatibility
- `recall::python` / `recall::require_python` / `$RECALL_PY` still work via a
  generated shim at `$PLUGIN_DATA/bin/uv-python-shim` so v0.6.2-era hook code
  keeps running without edits across the version-bump boundary. New code
  should call `recall::run` directly.

### Why this matters
- Marketplace install is now truly zero-prereq for the user. `bash` and `curl`
  are universal on every modern unix; uv handles every python concern after.
- First fire on a fresh box: ~30s while uv downloads python 3.12 + resolves
  deps. Every subsequent fire: <1s (cached venv).
- The bundled `uv` lives under `$CLAUDE_PLUGIN_DATA`, so it survives plugin
  updates (`$CLAUDE_PLUGIN_ROOT` is wiped on update; `$CLAUDE_PLUGIN_DATA` is
  not).

## [0.6.2] - 2026-05-26

### Fixed
- **Critical: Fresh marketplace installs were broken.** Hooks invoked bare
  `python3 -m total_recall ...` assuming the user had `pip install total-recall`
  system-wide. Marketplace installs don't run pip — so on a fresh install every
  hook silently failed with `No module named total_recall`, no DB was ever
  built, and `/total-recall:recall-health` reported zero ingest runs despite
  hooks firing dozens of times. Visible only in `logs/bootstrap.log`.

### Added
- `recall::python` resolver in `hooks/lib/common.sh`. Order of preference:
  1. `$RECALL_PYTHON` env override
  2. `$PLUGIN_ROOT/.venv/bin/python3` if it exists and can import `total_recall`
  3. System `python3` if it can import `total_recall`
  4. Auto-build `$PLUGIN_ROOT/.venv` via `python3 -m venv` + pip install
     of the plugin from its own source tree (one-time, ~30-60s, cached)
- `recall::_find_python310` searches `python3.10..3.13` on `PATH` and
  uv-managed pythons under `~/.local/share/uv/python/`, falling back to
  bare `python3` only if it is ≥ 3.10 (the plugin's `requires-python`).
- Every hook now resolves `RECALL_PY="$(recall::python)"` once after the
  `has_py` guard and uses `"$RECALL_PY"` instead of bare `python3` for all
  invocations (`-m total_recall index`, `-c "..."`, script paths, heredocs).

### Why this matters
- Plugin is now self-contained: a marketplace install runs end-to-end with no
  manual `pip install` step.
- On a fresh install, the first hook fire takes ~10-60s to build the venv;
  subsequent fires hit the cached venv with no overhead.
- Install requires only `python3.10+` on `PATH` (or via uv). On Ubuntu
  20.04+, `apt install python3.12 python3.12-venv` is the one-line setup.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.1] — 2026-05-26

### Changed
- First public GitHub release. v0.6.0 was tagged briefly during local
  preparation but never pushed; its commit SHAs were rewritten to purge an
  accidentally-committed `data/` directory containing a SQLite-indexed
  Cloudflare API token from old session-log mining tests. The token was
  rotated; GitHub Push Protection blocked the original push before any data
  reached the public repo. v0.6.1 is the first SHA-stable, externally-visible
  release.

### Carried forward from the unpushed v0.6.0

### Changed — license

- **Relicensed from MIT to `FSL-1.1-ALv2`** (Functional Source License,
  Version 1.1, Apache-2.0 Future License). Source remains visible; redistribution
  and modification remain permitted for any Permitted Purpose. A Competing Use —
  offering total-recall (or a substantially similar substitute) as a commercial
  product or service — is no longer a Permitted Purpose. Each released version
  automatically converts to the Apache License 2.0 on the second anniversary of
  its release date. See [`LICENSE.md`](LICENSE.md) for the full terms.
- `LICENSE` (MIT) removed in favor of `LICENSE.md` (FSL-1.1-ALv2).
- `pyproject.toml` license expression updated to `LicenseRef-FSL-1.1-ALv2`
  (PEP 639); `build-system.requires` bumped to `setuptools>=77` for PEP 639
  support; `license-files = ["LICENSE.md"]` declared explicitly.
- `.claude-plugin/plugin.json` license updated to `FSL-1.1-ALv2`.
- `MANIFEST.in` updated to include `LICENSE.md`.

### Rationale

- Pre-launch is the only clean window to seat the license. Setting the template
  now applies cleanly across the planned MCP portfolio.
- AGPL-3.0 was considered and ruled out: the `[vec]` extra (fastembed,
  huggingface-hub, tokenizers, hf-xet, flatbuffers) is Apache-2.0, which has
  documented patent-clause incompatibility with AGPL when forming a combined
  work.
- The two-year Apache-2.0 conversion is the safety valve: every release becomes
  fully open source on its second anniversary, so the historical record
  remains permanently auditable and forkable for non-competing uses.

## [0.5.0] — 2026-05-26

### Added — multi-CLI session adapters
- total-recall now mines transcripts from **8 clients**: Claude Code, OpenCode, Codex CLI, Gemini CLI, Cursor, Continue, Cline, Aider. One operator profile, served across every coding assistant on the machine.
- `lib/sources/` — `SessionSource` ABC, `SessionFile` dataclass, `SOURCES` registry, 8 adapters. Each self-registers at module import time.
- `total-recall sources {list,detect,enable,disable,test}` CLI for managing source detection.
- `docs/install/{claude_code,opencode,gemini_cli,codex,cursor,continue,cline,aider}.md` — per-CLI MCP install snippets, verified from live repos via repomix.
- `total-recall-mcp` console script for `uvx`-style zero-install (`uvx total-recall-mcp`).
- Multi-provider cost catalog: Anthropic + OpenAI (GPT-5 family, $1.25/$10) + Google (Gemini 2.5 family, $1.00/$10) verified against live 2026 pricing. Per-provider cache-read multipliers.
- Cross-source dedup: `(cwd, ts_minute_bucket, sha256(text[:200]))` with `claude_code > codex > opencode > gemini > continue > cline > cursor > aider` priority.
- `RELEASE.md` + `scripts/build-and-check.sh` + `scripts/publish-pypi.sh` + `MANIFEST.in` + `tests/test_packaging.py` for PyPI publishing.
- `.github/workflows/release.yml` — tag-triggered build → PyPI trusted publish → GitHub release.
- `.github/workflows/ci.yml` — 3 Python × 2 OS matrix + packaging job.
- `docs/marketplace.md` + `marketplace-entry.json` + `scripts/marketplace-install-local.sh` for 88plug marketplace.

### Changed
- Schema v3 → v4: `messages` + `extractions` gain `source` + `dedup_superseded_by_source`. Idempotent ALTER TABLE.
- `ingest_all` accepts `sources=[...]`; default walks every available source.
- `ClaudeCodeSource` honors caller-supplied `projects_root` (tests).
- `pyproject.toml` — `[project]` metadata for PyPI; `total-recall-mcp` script; `detector*` added to `packages.find`.

### Fixed
- OpenCode adapter: table names corrected from camelCase to lowercase snake_case (2026 Drizzle schema). Legacy fallback kept.
- `lib/sources/__init__.py` now imports all 8 adapters (was 2). `all_sources()` now sees the full set everywhere.

## [0.4.0] — 2026-05-25

### Added — continuous-fresh memory
- `detector/reinjection.py` — `should_reinject()` with hard/soft signal taxonomy; calibrated on real corpus (`escalation_risk>=5` solo hard trigger at 2.5% fire rate, `>=3` requires soft combo, `repetition_callout` always fires).
- `detector/outcomes.py` — Beta(α,β) per-trigger precision learning. 3-turn evaluation window.
- `hooks/lib/scope_detect.py` — 9-scope keyword vote + pivot regex + cwd-change.
- `hooks/lib/scope_delta.py` — 2KB priority-ordered delta payload.
- `hooks/lib/session_state.py` — per-session JSON state file.
- `hooks/pre-compact-seed.sh` + `hooks/post-compact-recovery.sh` — survive compaction.
- Incremental profile updates: `operator_profile`, `voice_profile`, `ontology` now update on every Stop hook.
- 90-day half-life confidence decay + append-supersede policy + `tentative_facts` table.
- `total-recall consolidate` weekly cron + systemd timer.
- `total-recall adaptive` per-trigger precision report.
- MCP tool `recall_targeted(intent, subject)` — Anthropic `memory_20250818` pattern with 7 routed intents.

### Decided
- Hybrid signal-driven + lazy MCP retrieval (industry consensus mem0 / Letta / Anthropic / A-Mem / BeliefMem).
- 3-6 re-injection events per session hard cap. Signal dilution is the constraint, not cost (~$0.015/session).
- Append-supersede never destructive-overwrite.

## [0.3.0] — 2026-05-25

### Added
- 10 new extractors covering operator-level signals: `bans`, `goals`, `standing_decisions`, `self_corrections`, `model_corrections`, `operator_profile`, `voice_profile`, `truth_rhetoric`, `away_summaries`, `secrets` (redaction).
- 16 new MCP tools across `mcp_server/extras/` — operator context, bans, goals, standing decisions, corrections recall, escalation assessment, rhetoric, voice, ontology lookups — bringing the total surface to 24 MCP tools.
- Six new slash commands: `/total-recall:recall-operator-context`, `/total-recall:recall-check-banned`, `/total-recall:recall-goal`, `/total-recall:recall-corrections`, `/total-recall:recall-decisions`, `/total-recall:recall-escalation`.
- `inspect --show-extractions` flag for listing every extraction row in a session.
- DAG / sidechain awareness in the walker (`tests/test_dag.py`, `tests/test_sidechain.py`).
- `away_summary` extraction surfaced verbatim by `/total-recall:recall-inspect` when present.

### Changed
- `recall-promote` slash command now always treats its argument as a topic (the previous `--id` path called a non-existent `total-recall query --id` flag).
- SessionStart signpost now sources operator context from `get_operator_context` instead of ad-hoc queries.

### Fixed
- `recall-inspect` invocation missing `--show-extractions`, which suppressed the "Top 5 extractions by score" output.
- CHANGELOG mislabel: prior `0.2.0` heading corrected to `0.2.1` (no `v0.2.0` tag exists).

## [0.2.1] — 2026-05-25

### Added
- `total-recall metrics` Click subcommand with `summary`, `cost`, `sessions`, `topics`, `health` sub-subcommands.
- SQLite schema v2: `turns`, `compactions`, `ingest_runs` tables — populated during ingest from `message.usage` and `compact_boundary` records.
- `total_recall.events` module: NDJSON event emitter with 10MB rotation, used by `metrics health`.
- `total_recall.cost` module: Anthropic model price catalog with cache-multiplier estimation.
- `/total-recall:metrics`, `/total-recall:cost`, `/total-recall:topics` slash commands.
- Initial plugin scaffold (`.claude-plugin/plugin.json`, MIT license, packaging metadata).
- Four-layer architecture: jsonl walker, extractors, SQLite FTS5 + sqlite-vec index, delivery surfaces.
- Five extractors: decisions, user corrections, failed approaches, progress markers, project facts.
- Delivery surfaces: SessionStart signpost hook, MCP `recall` tool, `/recall` skill, slash commands.
- Local-only storage at `~/.claude/plugins/data/total-recall/`.
- Optional vector recall via `fastembed` + `sqlite-vec` (install with `[vec]` extra).
- Developer tooling: `ruff`, `mypy`, `pytest` configured in `pyproject.toml`.

### Changed
- Schema bumped to v2 (idempotent migration on first open).

### Decided
- OpenTelemetry deferred to v0.3 pending upstream MCP SDK OTel middleware (issue #421).
- Langfuse evaluated and rejected (wrong abstraction: total-recall isn't an LLM caller).
