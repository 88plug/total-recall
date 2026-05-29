#!/usr/bin/env bash
# One-time setup for total-recall's optional local-LLM refinement layer.
# Idempotent — safe to re-run.
#
# 1. install ollama if missing (uses the official installer; needs sudo)
# 2. start the daemon if it isn't already running
# 3. pull the configured model (default: gemma4:e2b, ~7 GB)
# 4. drop a sentinel so the SessionStart detector stops nagging
#
# Env overrides:
#   TOTAL_RECALL_LLM_MODEL    — model tag to pull (default gemma4:e2b)
#   TOTAL_RECALL_LLM_BASE_URL — daemon URL (default http://localhost:11434)
#
# Privacy: this script only runs ollama / curl / sudo. It does NOT send any
# transcript content anywhere. The refinement layer it sets up is local-only
# by design — see README "Optional local-LLM refinement".

set -euo pipefail

MODEL="${TOTAL_RECALL_LLM_MODEL:-gemma4:e2b}"
BASE_URL="${TOTAL_RECALL_LLM_BASE_URL:-http://localhost:11434}"
DATA_ROOT="${CLAUDE_PLUGIN_DATA:-${HOME}/.local/share/total-recall-88plug}"
DATA_ROOT="${DATA_ROOT%/}/total-recall"
SENTINEL="${DATA_ROOT}/.ollama_ready"

mkdir -p "$DATA_ROOT"

log() { printf '[llm-setup] %s\n' "$*"; }

# ----- 1. ollama binary ------------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
  log "ollama already installed at $(command -v ollama)"
else
  log "ollama not installed — running official installer (https://ollama.com/install.sh)"
  log "this requires sudo and will write to /usr/local/bin"
  curl -fsSL https://ollama.com/install.sh | sh
fi

# ----- 2. daemon -------------------------------------------------------------
if curl -sf "${BASE_URL}/api/tags" >/dev/null 2>&1; then
  log "ollama daemon already reachable at ${BASE_URL}"
else
  log "starting ollama daemon (background, log: /tmp/ollama-serve.log)"
  nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    curl -sf "${BASE_URL}/api/tags" >/dev/null 2>&1 && break
    [ "$i" = "10" ] && { log "FATAL: daemon failed to start within 10s — see /tmp/ollama-serve.log"; exit 1; }
  done
  log "daemon up"
fi

# ----- 3. model --------------------------------------------------------------
if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "${MODEL}\(:latest\)\?"; then
  log "model ${MODEL} already pulled"
else
  log "pulling ${MODEL} — may take several minutes (~GB-scale download)"
  ollama pull "${MODEL}"
fi

# ----- 4. sentinel -----------------------------------------------------------
touch "$SENTINEL"
log "sentinel written: ${SENTINEL}"

# ----- 5. self-test ----------------------------------------------------------
log "smoke-testing the refinement client against the live model..."
if RECALL_PY="$(command -v python3.11 || command -v python3)" && [ -n "$RECALL_PY" ]; then
  "$RECALL_PY" - <<PYEOF || { log "smoke test FAILED — model installed but refinement is not yet callable"; exit 1; }
import os, sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT:-$(pwd)}")
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
