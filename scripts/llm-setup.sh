#!/usr/bin/env bash
# One-time (idempotent) setup for total-recall's optional local-LLM refinement layer.
# Now delegates binary resolution, daemon start, and model pull to the shared
# functions in hooks/lib/common.sh — the same code path used by the automatic
# first-run bootstrap.
#
# Env overrides:
#   TOTAL_RECALL_LLM_MODEL       — model tag to pull (default: qwen3.5:2b)
#   TOTAL_RECALL_LLM_BASE_URL    — daemon URL (default: http://localhost:11434)
#   TOTAL_RECALL_LLM_PROVIDER    — set to "none" to skip (same opt-out as auto path)
#   RECALL_OLLAMA                — explicit path to ollama binary (skips auto-resolve)
#   CLAUDE_PLUGIN_DATA           — plugin data root (harness sets this; falls back to
#                                  ~/.claude/plugins/data)
#
# Privacy: this script only runs ollama/curl. It does NOT send transcript
# content anywhere. The refinement layer it sets up is local-only by design.
#
# Default (no-sudo) path:
#   - Tries ollama on PATH, /snap/bin/ollama, then fetches the CPU tarball
#     from https://ollama.com/download/ollama-linux-{amd64,arm64}.tgz into
#     ${RECALL_DATA_ROOT}/bin/ollama — no sudo, no systemd.
# Sudo fallback:
#   - If the no-sudo fetch fails AND sudo is available, offers the official
#     install.sh as a fallback (writes to /usr/local/bin).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_SH="$SCRIPT_DIR/../hooks/lib/common.sh"
if [ ! -f "$COMMON_SH" ]; then
  printf '[llm-setup] ERROR: cannot find hooks/lib/common.sh at %s\n' "$COMMON_SH" >&2
  exit 1
fi
# shellcheck source=../hooks/lib/common.sh
source "$COMMON_SH"

log() { printf '[llm-setup] %s\n' "$*"; }

# Opt-out guard (same as recall::provision_llm).
LLM_PROVIDER="${TOTAL_RECALL_LLM_PROVIDER:-auto}"
if [ "$LLM_PROVIDER" = "none" ]; then
  log "TOTAL_RECALL_LLM_PROVIDER=none — skipping (remove or change to 'auto' to enable)"
  exit 0
fi

MODEL="${TOTAL_RECALL_LLM_MODEL:-qwen3.5:2b}"
BASE_URL="${TOTAL_RECALL_LLM_BASE_URL:-http://localhost:11434}"
SENTINEL="${RECALL_DATA_ROOT}/.ollama_ready"

mkdir -p "$RECALL_DATA_ROOT" "$RECALL_LOG_DIR"

log "model=$MODEL  base_url=$BASE_URL"

# ----- 1. ollama binary (no-sudo path via shared resolver) -------------------
log "resolving ollama binary..."
OLLAMA_BIN=""
if OLLAMA_BIN="$(recall::ollama)"; then
  log "ollama resolved: $OLLAMA_BIN ($("$OLLAMA_BIN" --version 2>/dev/null || echo '?'))"
else
  log "WARN: no-sudo resolution failed."
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    log "sudo available — falling back to official installer (https://ollama.com/install.sh)"
    log "this writes to /usr/local/bin"
    curl -fsSL https://ollama.com/install.sh | sudo sh
    OLLAMA_BIN="$(command -v ollama 2>/dev/null || true)"
    if [ -z "$OLLAMA_BIN" ]; then
      log "ERROR: official installer ran but ollama still not found on PATH"
      exit 1
    fi
    # Prefer the just-installed binary for subsequent shared-lib lookups.
    export RECALL_OLLAMA="$OLLAMA_BIN"
  else
    log "ERROR: could not resolve ollama and sudo unavailable. Install ollama manually:"
    log "  curl -fsSL https://ollama.com/install.sh | sh   (needs sudo)"
    log "  OR: set RECALL_OLLAMA=/path/to/ollama"
    exit 1
  fi
fi

# ----- 2. daemon (shared function, runs in foreground for llm-setup) ---------
log "checking daemon at ${BASE_URL}..."
if curl -sf "${BASE_URL}/api/tags" >/dev/null 2>&1; then
  log "daemon already reachable at ${BASE_URL}"
else
  log "starting daemon (background, log: ${RECALL_LOG_DIR}/llm-provision.log)"
  recall::ollama_serve || {
    log "ERROR: daemon failed to start — see ${RECALL_LOG_DIR}/llm-provision.log"
    exit 1
  }
fi

# ----- 3. model (shared function) -------------------------------------------
log "checking model ${MODEL}..."
recall::ollama_pull "$MODEL" || {
  log "ERROR: model pull failed — see ${RECALL_LOG_DIR}/llm-provision.log"
  exit 1
}
log "model ${MODEL} ready"

# ----- 4. sentinel -----------------------------------------------------------
touch "$SENTINEL"
log "sentinel written: ${SENTINEL}"

# ----- 5. smoke test ---------------------------------------------------------
log "smoke-testing the refinement client against the live model..."
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
if command -v python3 >/dev/null 2>&1; then
  python3 - <<PYEOF || { log "smoke test FAILED — model installed but refinement not callable"; exit 1; }
import os, sys
sys.path.insert(0, "${PLUGIN_ROOT}")
try:
    from extractors.llm.client import get_default_client
except ImportError:
    print("[llm-setup] extractors.llm.client not on PYTHONPATH; skipping smoke test")
    sys.exit(0)
c = get_default_client()
print(f"[llm-setup] client.available={c.available} model={c.model}")
if not c.available:
    sys.exit(1)
out = c.generate_json(system="Reply with JSON only.", user='Return {"ok": true}.', schema={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]})
print(f"[llm-setup] smoke output: {out}")
if out is None or not out.get("ok"):
    sys.exit(1)
PYEOF
fi

log "done. total-recall LLM refinement layer is active."
log "next: run /total-recall:recall-rebuild so the next rebuild includes refinement."
