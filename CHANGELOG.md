# Changelog

All notable changes to this project will be documented in this file.

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
