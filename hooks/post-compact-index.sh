#!/usr/bin/env bash
# PostCompact hook (async). Same indexer drive as stop-index.sh — kept as a
# separate file so we can tune compaction-specific behavior later (e.g. full
# rescan of the compacted session) without touching the Stop path.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
CWD=""
SESSION_ID=""

trap 'recall::log "post-compact-index: unexpected exit ($?)"; recall::log_json hook.post_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" error="trap"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
SESSION_ID="$(recall::field session_id)"

if ! recall::has_py; then
  recall::log "post-compact-index: python3 missing; skipping"
  exit 0
fi

# Resolve usable python (uses bundled venv or autobuilds one). Sets $RECALL_PY.
if ! RECALL_PY="$(recall::python)"; then
  recall::log "$(basename "${BASH_SOURCE[0]}" .sh): could not resolve python with total_recall; skipping"
  exit 0
fi

REPO_ROOT="$(cd "$HOOK_DIR/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# Same first-run path as stop-index.sh: bootstrap if no DB yet, skip if a
# bootstrap is already running.
if recall::is_fresh_install && ! recall::bootstrap_in_progress; then
  recall::start_bootstrap "PostCompact"
  recall::log_json hook.post_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" bootstrap="started"
  exit 0
fi
if recall::bootstrap_in_progress; then
  recall::log "post-compact-index: bootstrap in progress; skipping incremental tick"
  recall::log_json hook.post_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" bootstrap="waiting"
  exit 0
fi

# Detach the incremental ingest so the PostCompact event returns immediately
# and the indexer is never raced against the 60s hook deadline. The old
# foreground `timeout 55s ... index --since-last-tick` SIGTERM'd heavy scans
# mid-write, stalling the ingest_state watermark — see
# recall::start_incremental_index. The sibling post-compact-recovery.sh handles
# re-injection. PYTHONPATH (exported above) is inherited by the detached child.
recall::start_incremental_index "$RECALL_PY" "PostCompact"
recall::log "post-compact-index: detached tick dispatched"
recall::log_json hook.post_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" detached="true"
exit 0
