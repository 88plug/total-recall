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

TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="timeout 55s"
fi

rc=0
if "$RECALL_PY" -c 'import total_recall' >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  $TIMEOUT_BIN "$RECALL_PY" -m total_recall index --since-last-tick \
    >/dev/null 2>&1 || rc=$?
else
  # shellcheck disable=SC2086
  $TIMEOUT_BIN "$RECALL_PY" - <<'PY' >/dev/null 2>&1 || rc=$?
import sys
try:
    from index.db import connect
    from index.ingest import ingest_all  # type: ignore[import-not-found]
except Exception:
    sys.exit(0)
try:
    ingest_all(connect())
except Exception:
    sys.exit(0)
PY
fi

if [ "$rc" -ne 0 ]; then
  recall::log "post-compact-index: indexer rc=$rc"
fi
recall::log "post-compact-index: tick complete"
recall::log_json hook.post_compact elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" rc="$rc"
exit 0
