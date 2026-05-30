#!/usr/bin/env bash
# Stop hook (async). Re-indexes session transcripts in the background after a
# turn finishes. Async + 60s timeout means we never make the user wait. If the
# indexer CLI module isn't available yet, fall back to invoking the ingest
# function inline; if even that fails, log + exit 0.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
CWD=""
SESSION_ID=""

trap 'recall::log "stop-index: unexpected exit ($?)"; recall::log_json hook.stop elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" error="trap"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
SESSION_ID="$(recall::field session_id)"

if ! recall::has_py; then
  recall::log "stop-index: python3 missing; skipping"
  exit 0
fi

# Resolve usable python (uses bundled venv or autobuilds one). Sets $RECALL_PY.
if ! RECALL_PY="$(recall::python)"; then
  recall::log "$(basename "${BASH_SOURCE[0]}" .sh): could not resolve python with total_recall; skipping"
  exit 0
fi

# Resolve repo root so the python imports find `index/`.
REPO_ROOT="$(cd "$HOOK_DIR/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# First-run path: if the DB doesn't exist yet (user just enabled the plugin
# mid-session), detach a full backfill into the background and return
# immediately. The bootstrap process survives the hook's timeout because
# setsid puts it in its own session.
if recall::is_fresh_install && ! recall::bootstrap_in_progress; then
  recall::start_bootstrap "Stop"
  recall::log_json hook.stop elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" bootstrap="started"
  exit 0
fi

# If a bootstrap is still running, skip the incremental tick — the full
# backfill is doing all the work we'd otherwise duplicate.
if recall::bootstrap_in_progress; then
  recall::log "stop-index: bootstrap in progress; skipping incremental tick"
  recall::log_json hook.stop elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" bootstrap="waiting"
  exit 0
fi

# Detach the incremental ingest so the Stop event returns immediately and the
# indexer is never raced against the 60s hook deadline. The old foreground
# `timeout 55s ... index --since-last-tick` SIGTERM'd heavy scans mid-write,
# stalling the ingest_state watermark — see recall::start_incremental_index.
# PYTHONPATH (exported above) is inherited by the detached child.
recall::start_incremental_index "$RECALL_PY" "Stop"
recall::log "stop-index: detached tick dispatched"
recall::log_json hook.stop elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" detached="true"
exit 0
