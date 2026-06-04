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
TRANSCRIPT="$(recall::field transcript_path)"

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

if [ -z "${CTX// }" ] && { [ -z "${TRANSCRIPT:-}" ] || [ ! -f "${TRANSCRIPT:-}" ]; }; then
  recall::log "pre-compact-seed: no context to seed (cwd=$CWD)"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER"
  exit 0
fi

# --- Continuation packet (in-flight + durable state) ---------------------
# A second, transcript-derived lane: open files, last actions, the pending
# plan, the last directive, plus durable index state (active goal, decisions,
# failed attempts). Built by hooks/lib/build_packet.py. Every step below is
# optional and graceful — a failure here must never block compaction.
PACKET_JSON=""
PACKET_RENDER=""
if [ -n "${TRANSCRIPT:-}" ] && [ -f "$TRANSCRIPT" ]; then
  DB_PATH="${RECALL_DATA_ROOT}/index.db"
  if command -v timeout >/dev/null 2>&1; then
    PACKET_JSON="$(timeout 4s "$RECALL_PY" "$HOOK_DIR/lib/build_packet.py" \
      --transcript "$TRANSCRIPT" --session "$SESSION_ID" --cwd "$CWD" \
      --db "$DB_PATH" --max-chars 2000 2>/dev/null || true)"
  else
    PACKET_JSON="$("$RECALL_PY" "$HOOK_DIR/lib/build_packet.py" \
      --transcript "$TRANSCRIPT" --session "$SESSION_ID" --cwd "$CWD" \
      --db "$DB_PATH" --max-chars 2000 2>/dev/null || true)"
  fi
fi

if [ -n "${PACKET_JSON// }" ]; then
  # (b) Persist the packet to the session-state dir as
  #     <session_id>.continuation.json so post-compact-recovery + the
  #     SessionStart restore hook can find it deterministically.
  STATE_KEY="${SESSION_ID:-$(recall::cwd_slug "$CWD")}"
  STATE_DIR="${RECALL_DATA_ROOT}/sessions"
  if mkdir -p "$STATE_DIR" 2>/dev/null; then
    CONT_FILE="${STATE_DIR}/${STATE_KEY}.continuation.json"
    TMP_CONT="${CONT_FILE}.tmp.$$"
    printf '%s' "$PACKET_JSON" > "$TMP_CONT" 2>/dev/null \
      && mv "$TMP_CONT" "$CONT_FILE" 2>/dev/null \
      || rm -f "$TMP_CONT" 2>/dev/null
  fi

  # (d) Drop a single overwriteable memory markdown so Claude Code re-attaches
  #     the in-flight state automatically post-compaction. Only when the
  #     project's memory dir already exists (we never create it).
  MEM_SLUG="$(recall::cwd_slug "$CWD")"
  MEM_DIR="${HOME}/.claude/projects/${MEM_SLUG}/memory"
  if [ -d "$MEM_DIR" ]; then
    {
      printf '%s\n\n' '# total-recall continuation (auto-generated)'
      printf '%s\n\n' '_Last in-flight + durable state captured at compaction. Overwritten each compaction; safe to delete._'
      printf '```json\n%s\n```\n' "$PACKET_JSON"
    } > "${MEM_DIR}/total-recall-continuation.md" 2>/dev/null || true
  fi

  # (c) Render the packet for inclusion in the PRESERVE-VERBATIM block.
  PACKET_RENDER="$(printf '%s' "$PACKET_JSON" | "$RECALL_PY" -c '
import json, sys
sys.path.append(__import__("os").environ.get("CLAUDE_PLUGIN_ROOT",""))
try:
    from extractors.continuation_packet import render_continuation_packet
    data = json.loads(sys.stdin.read() or "{}")
    out = render_continuation_packet(data, max_chars=4000)
    if out:
        sys.stdout.write(out)
except Exception:
    pass
' 2>/dev/null || true)"
fi

# Wrap the JSON payload in a short marker so the summarizer recognizes this
# block as durable operator state and PRESERVES it verbatim in the summary
# (rather than paraphrasing it into uselessness). The continuation packet
# rides inside the same marker block when present.
SEED_TEXT=""
if [ -n "${CTX// }" ]; then
  SEED_TEXT=$(printf '%s\n%s\n%s\n' \
    '[total-recall] OPERATOR CONTEXT — PRESERVE VERBATIM IN POST-COMPACT SUMMARY:' \
    "$CTX" \
    '[total-recall] END OPERATOR CONTEXT')
fi
if [ -n "${PACKET_RENDER// }" ]; then
  CONT_BLOCK=$(printf '%s\n%s\n%s\n' \
    '[total-recall] CONTINUATION (in-flight state) — PRESERVE VERBATIM:' \
    "$PACKET_RENDER" \
    '[total-recall] END CONTINUATION')
  if [ -n "$SEED_TEXT" ]; then
    SEED_TEXT="${SEED_TEXT}"$'\n\n'"${CONT_BLOCK}"
  else
    SEED_TEXT="$CONT_BLOCK"
  fi
fi

# Both lanes empty (operator context AND packet) -> nothing to seed.
if [ -z "${SEED_TEXT// }" ]; then
  recall::log "pre-compact-seed: nothing to seed after packet build (cwd=$CWD)"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER"
  exit 0
fi

recall::emit_context "$SEED_TEXT" "PreCompact" || {
  recall::log "pre-compact-seed: emit failed (cwd=$CWD)"
  recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER" error="emit_failed"
  exit 0
}

EMITTED="true"
recall::log "pre-compact-seed: seeded ${#SEED_TEXT} chars (cwd=$CWD trigger=$TRIGGER)"
recall::log_json hook.pre_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" trigger="$TRIGGER" bytes="${#SEED_TEXT}"
exit 0
