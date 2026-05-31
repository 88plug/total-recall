# Known Issues

Tracked defects from the v0.12.0 full-surface verification sweep (every CLI
subcommand, MCP boot, hooks, config plumbing, slash commands, skills). The sweep
ran 6 probers over surfaces the in-process unit suite never exercised.

**Root cause of the test miss:** the ~1280-test suite runs entirely in-process
(import a module, call a function). Nothing booted a surface the way the CLI or
plugin harness actually does, so config-launch bugs, cross-module API drift, and
doc↔code drift were all invisible. v0.13.0 added `test_cli_contracts.py` and
`test_version_consistency.py` to start closing that gap.

## Fixed in v0.13.0

- ✅ **CRITICAL** `total-recall tail` crashed every call — `cmd_tail` passed
  `db_path=`/`cwd_filter=` to `tail_loop(conn, interval, projects_root, …)`.
- ✅ **CRITICAL** `total-recall tail --once` crashed — `ingest_all(full=False)`;
  the kwarg is `force_full`.
- ✅ `.mcp.json` dev-checkout fallback (`${CLAUDE_PLUGIN_ROOT:-.}`); removed a
  bad `TOTAL_RECALL_DB_DIR=${CLAUDE_PLUGIN_DATA:-./data}` default that blocked
  the server's `~/.local/share` fallback.
- ✅ `marketplace-entry.json` version drift (0.9.0 → current).

## Fixed in v0.13.1

- ✅ **HIGH** Partial-schema CLI crashes (`stats`/`dump`/`inspect`/`metrics
  summary`/`metrics sessions`) — a DB with only `reinjection_outcomes` (created
  by `adaptive`) made read-only query commands hit `no such table: messages` and
  exit 2 with an internal error. `__main__.main()` now catches
  `sqlite3.OperationalError: no such table` centrally → clean "index has not been
  built; run `index --full`" message + exit 1. Test: `test_partial_schema.py`.
- ✅ Slash-command `--json` flag position — `recall-status.md` / `recall-inspect.md`
  put the global `--json` after the subcommand (`No such option`). Moved before it.
- ✅ `skills/llm-setup/SKILL.md` dead link `docs/install/llm-refinement.md` →
  `docs/llm-refinement.md`.
- ✅ Stale `gemma4:e2b` default refs (SKILL.md, pyproject `[llm]` comment) →
  `qwen3.5:2b`.
- ✅ Removed leftover executable `hooks/session-start-signpost-v1.sh.bak`.

## Fixed in v0.13.3

- ✅ **HIGH** MCP `check_banned` / `list_failed_attempts` crashed on the
  read-only conn (`ensure_schema` CREATE on `mode=ro`). Read paths now use a
  `_table_exists` guard, never write. Test: `tests/test_bans_readonly.py`.
  (Verified no other index module has the same read-path-writes-schema pattern —
  `bans.py` was the only one.)

## Open — real, ranked

## Fixed in v0.13.5

- ✅ #5 incremental-index tables (`standing_decisions`/`bans`/`failed_attempts`/
  `goal_stack`) created empty by `apply_schema` → MCP tools return `[]` not a
  "reindex" notice.
- ✅ #10 retargeted 2 perma-skipped `test_operator_context` tests to the real
  `session-start-signpost.sh`; both run + pass.
- ✅ #11 `hooks.json` uses `${CLAUDE_PLUGIN_ROOT:-.}` in all 6 command paths.

## Fixed in v0.13.6

- ✅ #6 UserPromptSubmit hook populated-DB path now has real e2e coverage
  (`tests/test_hooks_dispatch.py`) — drives the hook subprocess against a
  >100KB synthetic DB so the decide_and_format dispatch path runs, replacing the
  structurally-broken `test_hooks.sh` [4]/[6] sections (0-byte DB → bootstrap).

## Open — real, ranked

### MEDIUM

4. **`get_machine_inventory` returns garbage.** ~61k rows of tokenized cwd path
   slugs misidentified as hostnames (role/ip/gpu all empty). The ontology NER
   pass over session paths populates `machines` with path components. Fix: tighten
   the machine-name heuristic / exclude path-slug tokens.

5. **`list_standing_decisions` / `get_decision_for_topic` error on incremental
   index.** `standing_decisions`/`bans`/`failed_attempts`/`goal_stack` tables are
   only created during a full rebuild by their extractors; a plain incremental
   ingest never creates them, so these tools return "table not present; reindex".
   Fix: create these tables in `apply_schema` (empty) so the tools degrade to an
   empty result rather than an error.

6. **`test_hooks.sh` sections [4] and [6] are structurally broken.** Both create
   a 0-byte `index.db`, but `is_fresh_install()` treats <102400 bytes as fresh →
   the hook takes the bootstrap path and never reaches the populated-DB / signpost
   / `decide_and_format.py` code under test. The primary UserPromptSubmit pipeline
   has no effective end-to-end coverage. Fix: use a ≥200KB synthetic DB and assert
   the populated path (recommend a `test_hooks_dispatch.py` in pytest).

### LOW

10. **`test_operator_context.py` 2 tests permanently skip** — they target
    `hooks/session-start-signpost-v2.sh`, which doesn't exist (the live file is
    `session-start-signpost.sh`). False confidence. Fix or delete.

11. **`hooks/hooks.json` uses unguarded `${CLAUDE_PLUGIN_ROOT}`** in all 6 command
    paths. Lower severity than the `.mcp.json` case: the harness *always* sets
    `CLAUDE_PLUGIN_ROOT` when it runs plugin hooks, so this never fails in a real
    install. Adding a `:-` default would be defensive consistency only.

## Rejected — false positives

- ❌ **"hooks/lib missing from the wheel breaks UserPromptSubmit."** The plugin is
  distributed by **git clone** (marketplace source = `github: 88plug/total-recall`),
  not by `pip install`. Hooks resolve their Python libs via
  `HOOK_DIR="$(dirname "${BASH_SOURCE[0]}")"` — relative to themselves inside
  `CLAUDE_PLUGIN_ROOT` (the clone), where `hooks/lib/` is fully present and
  `hooks/lib/__init__.py` **is** tracked. The wheel is only the `pip install
  total-recall` CLI path, which doesn't run hooks. No fix needed.
