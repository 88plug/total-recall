#!/usr/bin/env bash
# SessionStart hook v2 — unified-context signpost.
#
# Replaces v1's hand-rolled "what's in memory" markdown with the JSON
# payload produced by ``mcp_server.extras.operator_context_tools.get_operator_context``.
# One call, ~1800-char ceiling, JSON-shaped so the model can parse it.
#
# Same guarantees as v1:
#   - never blocks the session (exit 0 on every error path)
#   - silent (no stdout) when the index is missing or the payload empty
#   - 5s wall-clock budget; aborts and exits 0 on overrun
#   - first-run bootstrap detection (kicks off the backfill, emits a banner
#     exactly once, then exits silently from then on)
#
# The orchestrator decides when to swap this in for v1 (probably by updating
# hooks.json or replacing the existing session-start-signpost.sh).

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
EMITTED="false"

trap 'recall::log "session-start-v2: unexpected exit ($?)"; recall::log_json hook.session_start elapsed_ms="$(recall::elapsed_ms)" cwd="${CWD:-}" session_id="${SESSION_ID:-}" emitted="$EMITTED" error="trap" version="v2"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
[ -n "$CWD" ] || CWD="${CLAUDE_PROJECT_DIR:-${PWD:-/unknown}}"
SESSION_ID="$(recall::field session_id)"

if ! recall::has_jq || ! recall::has_py; then
  recall::log "session-start-v2: missing jq or python3; skipping (cwd=$CWD)"
  exit 0
fi

# Resolve usable python (uses bundled venv or autobuilds one). Sets $RECALL_PY.
if ! RECALL_PY="$(recall::python)"; then
  recall::log "$(basename "${BASH_SOURCE[0]}" .sh): could not resolve python with total_recall; skipping"
  exit 0
fi

# First-run bootstrap — same logic as v1 because the bootstrap message must
# fire regardless of which signpost variant is active.
if recall::is_fresh_install; then
  recall::start_bootstrap "SessionStart"
  MARKER="${RECALL_DATA_ROOT}/.bootstrap_banner_shown"
  if [ ! -f "$MARKER" ]; then
    BANNER="$(recall::bootstrap_banner SessionStart)"
    recall::emit_context "$BANNER" "SessionStart" \
      && touch "$MARKER" 2>/dev/null \
      && EMITTED="true"
    recall::log "session-start-v2: first-run bootstrap banner emitted"
  fi
  recall::log_json hook.session_start elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" bootstrap="started" version="v2"
  exit 0
fi

# Invoke the MCP tool's Python implementation directly (no MCP transport
# needed — we already have the module on PYTHONPATH and the DB resolution
# logic in mcp_server.server picks up $TOTAL_RECALL_DB_DIR / $CLAUDE_PLUGIN_DATA).
#
# We deliberately *don't* spin up the FastMCP stdio server here: a child
# Python process that imports the module and calls the function is an order
# of magnitude faster and avoids the JSON-RPC handshake overhead.
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
# Skip the "index missing" error sentinel — same as v1: silent on no-DB.
if isinstance(payload, dict) and payload.get("error"):
    sys.exit(0)
# Drop the internal _kind / _cwd tags from the wire payload (they cost ~30
# chars and only matter to debug log readers).
if isinstance(payload, dict):
    payload.pop("_kind", None)
    payload.pop("_cwd", None)
if not payload:
    sys.exit(0)
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
'

CTX=""
if command -v timeout >/dev/null 2>&1; then
  CTX="$(timeout 5s "$RECALL_PY" -c "$PY_SNIPPET" -- "$CWD" 2>/dev/null || true)"
else
  CTX="$("$RECALL_PY" -c "$PY_SNIPPET" -- "$CWD" 2>/dev/null || true)"
fi

if [ -z "${CTX// }" ]; then
  recall::log "session-start-v2: no context (cwd=$CWD session=$SESSION_ID)"
  recall::log_json hook.session_start elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" version="v2"
  exit 0
fi

recall::emit_context "$CTX" "SessionStart" || {
  recall::log "session-start-v2: emit failed (cwd=$CWD)"
  recall::log_json hook.session_start elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" error="emit_failed" version="v2"
  exit 0
}

EMITTED="true"
recall::log "session-start-v2: context emitted (${#CTX} chars cwd=$CWD session=$SESSION_ID)"
recall::log_json hook.session_start elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="$EMITTED" version="v2"
exit 0
