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

# Decide which python invocation to use: WT-8's CLI if importable, else the
# inline fallback that drives ingest_all directly. Both paths are wrapped in a
# 55s timeout (slightly under the 60s hook budget to give us a clean exit).
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
  recall::log "stop-index: indexer rc=$rc (timeout=$([ -n "$TIMEOUT_BIN" ] && echo on || echo off))"
fi
recall::log "stop-index: tick complete"
recall::log_json hook.stop elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" emitted="false" rc="$rc"
exit 0
