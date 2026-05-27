#!/usr/bin/env bash
# PreCompact hook — survive-compaction seed.
#
# The summarizer agent that builds the post-compact summary IS what we have to
# influence. Anything we feed via additionalContext on PreCompact is mixed into
# the summarizer's input, so persistent operator context (identity, active goal,
# standing decisions, bans) gets *baked into* the new summary rather than
# evaporating with the rest of the transcript.
#
# Companion: post-compact-recovery.sh flips a session-state flag so the very
# next UserPromptSubmit re-injects on top of the summary — belt-and-suspenders.
#
# Guarantees:
#   - 5-second wall-clock budget (matches hooks.json `timeout`)
#   - exit 0 on every error path (must never block compaction)
#   - silent stdout if there's nothing to seed
#   - logs to events.jsonl + hooks.log

set -uo pipefail  # NOT -e: we never want a stray failure to abort the seed

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
EMITTED="false"
CWD=""
SESSION_ID=""

trap 'recall::log "pre-compact-seed: unexpected exit ($?)"; recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" error="trap"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
[ -n "$CWD" ] || CWD="${CLAUDE_PROJECT_DIR:-${PWD:-/unknown}}"
SESSION_ID="$(recall::field session_id)"
TRIGGER="$(recall::field trigger)"

if ! recall::has_jq || ! recall::has_py; then
  recall::log "pre-compact-seed: missing jq or python3; skipping"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER" skipped="no_deps"
  exit 0
fi

# Resolve usable python (uses bundled venv or autobuilds one). Sets $RECALL_PY.
if ! RECALL_PY="$(recall::python)"; then
  recall::log "$(basename "${BASH_SOURCE[0]}" .sh): could not resolve python with total_recall; skipping"
  exit 0
fi

# Fresh-install path: no DB → nothing to seed. Don't kick a bootstrap here;
# compaction is the wrong moment to start a heavy backfill.
if recall::is_fresh_install; then
  recall::log "pre-compact-seed: fresh install, nothing to seed"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER" skipped="fresh"
  exit 0
fi

REPO_ROOT="$(cd "$HOOK_DIR/.." && pwd)"
# Append (don't prepend) so a user-provided PYTHONPATH wins — keeps tests
# able to inject shims and lets installed copies of the package override the
# in-tree source if the user explicitly set that up.
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}${REPO_ROOT}"

# Call get_operator_context directly via a child Python — same pattern as
# session-start-signpost.sh. The summarizer reads identity / active_goal /
# standing_decisions / bans straight off the JSON; the redundancy with
# SessionStart is *intentional* (compaction can fire mid-session, after
# SessionStart context has scrolled out of relevance).
PY_SNIPPET='
import json, sys
try:
    from mcp_server.extras.operator_context_tools import get_operator_context
except Exception as e:
    sys.stderr.write(f"get_operator_context import failed: {e}\n")
    sys.exit(0)
cwd = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
try:
    payload = get_operator_context(cwd=cwd)
except Exception as e:
    sys.stderr.write(f"get_operator_context call failed: {e}\n")
    sys.exit(0)
if isinstance(payload, dict) and payload.get("error"):
    sys.exit(0)
if isinstance(payload, dict):
    payload.pop("_kind", None)
    payload.pop("_cwd", None)
if not payload:
    sys.exit(0)
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
'

CTX=""
if command -v timeout >/dev/null 2>&1; then
  CTX="$(timeout 4s "$RECALL_PY" -c "$PY_SNIPPET" -- "$CWD" 2>/dev/null || true)"
else
  CTX="$("$RECALL_PY" -c "$PY_SNIPPET" -- "$CWD" 2>/dev/null || true)"
fi

if [ -z "${CTX// }" ]; then
  recall::log "pre-compact-seed: no context to seed (cwd=$CWD)"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER"
  exit 0
fi

# Wrap the JSON payload in a short marker so the summarizer recognizes this
# block as durable operator state and PRESERVES it verbatim in the summary
# (rather than paraphrasing it into uselessness).
SEED_TEXT=$(printf '%s\n%s\n%s\n' \
  '[total-recall] OPERATOR CONTEXT — PRESERVE VERBATIM IN POST-COMPACT SUMMARY:' \
  "$CTX" \
  '[total-recall] END OPERATOR CONTEXT')

recall::emit_context "$SEED_TEXT" "PreCompact" || {
  recall::log "pre-compact-seed: emit failed (cwd=$CWD)"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER" error="emit_failed"
  exit 0
}

EMITTED="true"
recall::log "pre-compact-seed: seeded ${#SEED_TEXT} chars (cwd=$CWD trigger=$TRIGGER)"
recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER" bytes="${#SEED_TEXT}"
exit 0
