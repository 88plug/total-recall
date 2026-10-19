#!/usr/bin/env bash
# SessionStart(compact) hook — deterministic post-compaction continuation.
#
# Counterpart to pre-compact-seed.sh. PreCompact additionalContext reaches the
# summarizer but survives unreliably; PostCompact cannot inject context at all.
# The one deterministic restore surface is SessionStart with matcher "compact",
# whose hookSpecificOutput.additionalContext is injected verbatim (10k cap).
#
# This hook loads the continuation packet pre-compact-seed.sh persisted to
# sessions/<session_id>.continuation.json (fallback: newest *.continuation.json
# for the same project_key within 24h), renders it compactly, and emits it as
# additionalContext so the model picks up exactly where it left off. It then
# clears the continuation_pending flag so the UserPromptSubmit bridge does not
# re-surface the same packet.
#
# Guarantees (same as session-start-signpost.sh):
#   - never blocks the session (exit 0 on every error path)
#   - silent (no stdout) when there is nothing to restore
#   - 5s wall-clock budget
#   - well under the 10k additionalContext cap

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
EMITTED="false"
CWD=""
SESSION_ID=""

trap 'recall::log "session-start-compact-restore: unexpected exit ($?)"; recall::log_json hook.session_start_compact elapsed_ms="$(recall::elapsed_ms)" cwd="${CWD:-}" session_id="${SESSION_ID:-}" emitted="$EMITTED" error="trap"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
[ -n "$CWD" ] || CWD="${CLAUDE_PROJECT_DIR:-${PWD:-/unknown}}"
SESSION_ID="$(recall::field session_id)"

if ! recall::has_jq || ! recall::has_py; then
  recall::log "session-start-compact-restore: missing jq or python3; skipping (cwd=$CWD)"
  exit 0
fi

if ! RECALL_PY="$(recall::python)"; then
  recall::log "$(basename "${BASH_SOURCE[0]}" .sh): could not resolve python; skipping"
  exit 0
fi

REPO_ROOT="$(cd "$HOOK_DIR/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}${REPO_ROOT}"

BODY=""
if command -v timeout >/dev/null 2>&1; then
  BODY="$(timeout 4s "$RECALL_PY" "$HOOK_DIR/lib/compact_restore.py" \
    --session "$SESSION_ID" --cwd "$CWD" --max-chars 6000 2>/dev/null || true)"
else
  BODY="$("$RECALL_PY" "$HOOK_DIR/lib/compact_restore.py" \
    --session "$SESSION_ID" --cwd "$CWD" --max-chars 6000 2>/dev/null || true)"
fi

# Real recall (operator context + indexed hits), not only the packet.
# SessionStart source=compact is the inject channel on Claude Code and Codex.
# The next UserPromptSubmit reinject stays as a backup.
RECALL_TEXT=""
DB_PATH="${RECALL_DATA_ROOT}/index.db"
if [ -f "$DB_PATH" ]; then
  if command -v timeout >/dev/null 2>&1; then
    RECALL_TEXT="$(timeout 4s "$RECALL_PY" -m total_recall --db "$DB_PATH" compact-recall \
      --cwd "$CWD" --session "$SESSION_ID" --max-chars 2500 \
      2>/dev/null || true)"
  else
    RECALL_TEXT="$("$RECALL_PY" -m total_recall --db "$DB_PATH" compact-recall \
      --cwd "$CWD" --session "$SESSION_ID" --max-chars 2500 \
      2>/dev/null || true)"
  fi
fi

CTX=""
if [ -n "${RECALL_TEXT// }" ]; then
  CTX="$RECALL_TEXT"
fi
if [ -n "${BODY// }" ]; then
  PACKET_BLOCK="$(printf '%s\n%s' \
    '[total-recall] POST-COMPACTION CONTINUATION — where we were:' \
    "$BODY")"
  if [ -n "$CTX" ]; then
    CTX="${CTX}"$'\n\n'"${PACKET_BLOCK}"
  else
    CTX="$PACKET_BLOCK"
  fi
fi

if [ -z "${CTX// }" ]; then
  recall::log "session-start-compact-restore: nothing to restore (cwd=$CWD session=$SESSION_ID)"
  recall::log_json hook.session_start_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED"
  exit 0
fi

if [ "${#CTX}" -gt 9000 ]; then
  CTX="${CTX:0:9000}"
fi

recall::emit_context "$CTX" "SessionStart" || {
  recall::log "session-start-compact-restore: emit failed (cwd=$CWD)"
  recall::log_json hook.session_start_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" error="emit_failed"
  exit 0
}

EMITTED="true"
recall::log "session-start-compact-restore: restored ${#CTX} chars (cwd=$CWD session=$SESSION_ID)"
recall::log_json hook.session_start_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" bytes="${#CTX}"
exit 0
