# Known Issues

Tracked defects from the v0.12.0 full-surface verification sweep (6 probers over
every CLI subcommand, MCP boot, hooks, config plumbing, slash commands, skills).

**Root cause of the original test miss:** the unit suite ran entirely in-process
(import a module, call a function with stubs). Nothing booted a surface the way
the CLI or plugin harness does, so config-launch bugs, cross-module API drift,
and doc↔code drift were all invisible. The follow-up releases added
`test_cli_contracts.py`, `test_version_consistency.py`, `test_partial_schema.py`,
`test_bans_readonly.py`, `test_hooks_dispatch.py`, and `test_ontology_hostname.py`
to close that class of gap.

## Status: all 17 sweep defects addressed (v0.13.0 → v0.14.0)

### Crash-on-use (HIGH) — fixed

- ✅ `total-recall tail` / `tail --once` crashed every call (cmd_tail↔tail_loop
  signature mismatch; `full=` vs `force_full=`). — **v0.13.0**
- ✅ `stats`/`dump`/`inspect`/`metrics summary`/`metrics sessions` exited 2 with
  `no such table` on a partial-schema DB. — **v0.13.2**
- ✅ MCP `check_banned` / `list_failed_attempts` crashed on the read-only
  connection (`ensure_schema` CREATE on `mode=ro`). — **v0.13.4**

### MEDIUM — fixed

- ✅ #5 incremental-only index returned a "table not present; reindex" notice for
  standing_decisions / bans / failed_attempts / goal_stack — tables now created
  empty by `apply_schema`. — **v0.13.5**
- ✅ #6 UserPromptSubmit hook populated-DB path had no e2e coverage — added
  `test_hooks_dispatch.py` (real subprocess, >100KB synthetic DB). — **v0.13.6**
- ✅ #4 `get_machine_inventory` returned ~61k garbage rows (cwd slugs as
  hostnames) — `_is_cwd_slug` guard in `_extract_machines_from_text`. — **v0.14.0**
  (Existing indexes need `total-recall rebuild` to purge already-stored rows.)

### Config / doc / LOW — fixed

- ✅ `.mcp.json` dev-checkout fallback uses plain `${CLAUDE_PLUGIN_ROOT}`. — **v0.13.0/.2**
- ✅ slash-command `--json` flag position (recall-status / recall-inspect). — doc batch
- ✅ #7 dead doc link `docs/install/llm-refinement.md` → `docs/llm-refinement.md`.
- ✅ #8 stale `gemma4:e2b` default refs → `qwen3.5:2b` (SKILL.md, pyproject).
- ✅ #9 removed leftover executable `hooks/session-start-signpost-v1.sh.bak`.
- ✅ #10 retargeted 2 perma-skipped `test_operator_context` tests to the real
  `session-start-signpost.sh` (now run + pass). — **v0.13.5**
- ✅ #11 `hooks/hooks.json` uses plain `${CLAUDE_PLUGIN_ROOT}` in all paths. — **v2.1.2**
  (Manifest token expansion does NOT interpret bash `${VAR:-default}`; any
  `:-default` fallback must live inside the shell scripts, not in the manifest.)
- ✅ `marketplace-entry.json` version drift (0.9.0 → current). — **v0.13.x**

## Rejected — false positive

- ❌ "hooks/lib missing from the wheel breaks UserPromptSubmit." The plugin is
  distributed by **git clone** (marketplace source = `github: 88plug/total-recall`),
  not `pip install`. Hooks resolve their Python libs via
  `HOOK_DIR="$(dirname "${BASH_SOURCE[0]}")"` inside `CLAUDE_PLUGIN_ROOT` (the
  clone), where `hooks/lib/` is fully present and tracked. The wheel is only the
  `pip install total-recall` CLI path, which never runs hooks. No fix needed.

## Future hardening (not defects — nice-to-have)

- A static linter over `commands/*.md` + `skills/*/SKILL.md` asserting every
  referenced file path exists and every CLI flag/subcommand is real, so doc↔code
  drift fails CI automatically.
