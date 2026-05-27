#!/usr/bin/env bash
# UserPromptSubmit hook (async).
#
# Conditionally injects highly-relevant prior memory for the current prompt.
# The decision is delegated to ``hooks/lib/decide_and_format.py``, which
# wires together the signal-driven re-injection pipeline:
#
#   * CW1: detector.reinjection.should_reinject  — fire/no-fire decision
#   * CW2: hooks.lib.scope_detect                — scope-shift detection
#   * CW3: hooks.lib.scope_delta                 — delta payload builder
#   * CW4: hooks.lib.session_state               — per-session state file
#
# This shell wrapper stays cheap and defensive: it filters out empty/short/
# stub prompts, bootstraps the index on a fresh install, then asks Python
# for a payload. Empty Python stdout = no envelope. The bash side never
# imports anything beyond `recall::*` from common.sh.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
EMITTED="false"
SESSION_ID=""
CWD=""

trap 'recall::log "user-prompt: unexpected exit ($?)"; recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" error="trap"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
[ -n "$CWD" ] || CWD="${CLAUDE_PROJECT_DIR:-${PWD:-/unknown}}"
SESSION_ID="$(recall::field session_id)"
PROMPT="$(recall::field prompt)"

# Cheap skips:
#   - empty or too-short prompts aren't worth searching for
#   - prompts starting with '<' are usually system/tool stubs
if [ -z "$PROMPT" ] || [ "${#PROMPT}" -lt 10 ]; then
  recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" skipped="short"
  exit 0
fi
case "$PROMPT" in
  '<'*)
    recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" skipped="stub"
    exit 0
    ;;
esac

if ! recall::has_jq || ! recall::has_py; then
  recall::log "user-prompt: missing jq or python3; skipping"
  recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" skipped="no_deps"
  exit 0
fi

# Resolve usable python (uses bundled venv or autobuilds one). Sets $RECALL_PY.
if ! RECALL_PY="$(recall::python)"; then
  recall::log "$(basename "${BASH_SOURCE[0]}" .sh): could not resolve python with total_recall; skipping"
  exit 0
fi

# First-run path: if no DB exists yet (user just enabled the plugin), kick
# the bootstrap and emit a one-time banner so they know recall will catch up.
# A one-shot marker prevents the banner from repeating on subsequent prompts
# while the bootstrap is still running.
if recall::is_fresh_install; then
  recall::start_bootstrap "UserPromptSubmit"
  MARKER="${RECALL_DATA_ROOT}/.bootstrap_banner_shown"
  if [ ! -f "$MARKER" ]; then
    BANNER="$(recall::bootstrap_banner UserPromptSubmit)"
    recall::emit_context "$BANNER" "UserPromptSubmit" \
      && touch "$MARKER" 2>/dev/null \
      && EMITTED="true"
    recall::log "user-prompt: emitted first-run bootstrap banner"
  fi
  recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" bootstrap="started"
  exit 0
fi

# Delegate the decision + payload formatting to Python. Python prints the
# payload to stdout (empty = no envelope). 5s timeout matches what the
# bootstrap path uses elsewhere — well inside the harness's hook budget.
PAYLOAD=""
if command -v timeout >/dev/null 2>&1; then
  PAYLOAD="$(timeout 5s "$RECALL_PY" "$HOOK_DIR/lib/decide_and_format.py" \
              --session "$SESSION_ID" \
              --cwd "$CWD" \
              --prompt "$PROMPT" \
              --db "${RECALL_DATA_ROOT}/index.db" \
              2>/dev/null || true)"
else
  PAYLOAD="$("$RECALL_PY" "$HOOK_DIR/lib/decide_and_format.py" \
              --session "$SESSION_ID" \
              --cwd "$CWD" \
              --prompt "$PROMPT" \
              --db "${RECALL_DATA_ROOT}/index.db" \
              2>/dev/null || true)"
fi

# Fallback: if the signal layer is missing or returned nothing, fall back
# to the legacy direct query. Keeps the hook useful for users on a build
# where CW1/2/3 haven't merged yet.
if [ -z "${PAYLOAD// }" ]; then
  if command -v timeout >/dev/null 2>&1; then
    PAYLOAD="$(timeout 7s "$RECALL_PY" "$HOOK_DIR/lib/query.py" prompt-relevant \
                --prompt "$PROMPT" --cwd "$CWD" --limit 3 --max-tokens 400 \
                2>/dev/null || true)"
  else
    PAYLOAD="$("$RECALL_PY" "$HOOK_DIR/lib/query.py" prompt-relevant \
                --prompt "$PROMPT" --cwd "$CWD" --limit 3 --max-tokens 400 \
                2>/dev/null || true)"
  fi
fi

if [ -z "${PAYLOAD// }" ]; then
  recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED"
  exit 0
fi

recall::emit_context "$PAYLOAD" "UserPromptSubmit" || {
  recall::log "user-prompt: emit failed"
  recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" error="emit_failed"
  exit 0
}

EMITTED="true"
recall::log "user-prompt: injected ${#PAYLOAD} chars (cwd=$CWD)"
recall::log_json hook.user_prompt_submit elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED"
exit 0
