#!/usr/bin/env bash
# PostCompact hook — survive-compaction recovery flag.
#
# Counterpart to pre-compact-seed.sh. While the seed bakes operator context
# INTO the post-compact summary, this hook flips a per-session JSON flag so
# detector.reinjection.should_reinject() sees `post_compact=True` on the next
# UserPromptSubmit. That makes user-prompt-retrieve.sh push fresh memory on
# top of the (possibly degraded) summary — belt-and-suspenders against the
# summarizer dropping the seed.
#
# Also re-primes the bootstrap data dir if compaction happens to coincide
# with an index version bump (we re-check is_fresh_install since the migration
# may have rotated index.db out from under us).
#
# Guarantees:
#   - 5-second budget
#   - exit 0 always
#   - state file: ${RECALL_DATA_ROOT}/session_state/<session_id>.json
#     (the convention SessionState consumers expect)

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HOOK_DIR/lib/common.sh"

recall::start_timer
CWD=""
SESSION_ID=""

trap 'recall::log "post-compact-recovery: unexpected exit ($?)"; recall::log_json hook.post_compact_recovery elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" error="trap"; exit 0' ERR

recall::read_input
CWD="$(recall::field cwd)"
[ -n "$CWD" ] || CWD="${CLAUDE_PROJECT_DIR:-${PWD:-/unknown}}"
SESSION_ID="$(recall::field session_id)"

# Fall back to a slug of cwd if no session_id (e.g. ad-hoc invocations); we
# still want the flag to land *somewhere* so the next UserPromptSubmit can
# read it.
STATE_KEY="${SESSION_ID:-$(recall::cwd_slug "$CWD")}"
STATE_DIR="${RECALL_DATA_ROOT}/session_state"
STATE_FILE="${STATE_DIR}/${STATE_KEY}.json"

mkdir -p "$STATE_DIR" 2>/dev/null || {
  recall::log "post-compact-recovery: mkdir $STATE_DIR failed"
  recall::log_json hook.post_compact_recovery elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" error="mkdir_failed"
  exit 0
}

NOW_ISO="$(date -u -Iseconds 2>/dev/null || date -u +%FT%TZ)"

# Merge into existing state if present (CW1's state writer may already have
# fields here we shouldn't clobber). Use jq for an atomic-ish read-merge-write;
# if jq is missing, fall back to writing a minimal JSON object — the flag
# survives, which is the load-bearing requirement.
WROTE="false"
if recall::has_jq; then
  TMP="${STATE_FILE}.tmp.$$"
  if [ -f "$STATE_FILE" ]; then
    # jq's `//` operator can't appear inside an object literal value position
    # without parens, and the parens form is awkward here — splitting into
    # an `+` then two `if/then/else` updates is cleaner and lets us preserve
    # existing cwd/session_id when the hook input omits them.
    jq --arg ts "$NOW_ISO" --arg cwd "$CWD" --arg sid "$SESSION_ID" \
       '. + {post_compact: true, post_compact_ts: $ts}
        | .cwd = (if $cwd != "" then $cwd else (.cwd // null) end)
        | .session_id = (if $sid != "" then $sid else (.session_id // null) end)' \
       "$STATE_FILE" > "$TMP" 2>/dev/null \
       && mv "$TMP" "$STATE_FILE" 2>/dev/null \
       && WROTE="true"
    rm -f "$TMP" 2>/dev/null
  else
    jq -n --arg ts "$NOW_ISO" --arg cwd "$CWD" --arg sid "$SESSION_ID" \
       '{post_compact: true, post_compact_ts: $ts, cwd: $cwd, session_id: $sid}' \
       > "$STATE_FILE" 2>/dev/null && WROTE="true"
  fi
fi

if [ "$WROTE" != "true" ]; then
  # jq-less / write-failed fallback. A naive overwrite is acceptable here:
  # losing other fields is strictly less bad than missing the post_compact
  # flag (which is the only thing that prevents the model from re-asking
  # questions answered before compaction).
  printf '{"post_compact":true,"post_compact_ts":"%s","cwd":"%s","session_id":"%s"}\n' \
    "$NOW_ISO" "$CWD" "$SESSION_ID" > "$STATE_FILE" 2>/dev/null && WROTE="true"
fi

# Re-prime the bootstrap marker if the index has been wiped/rotated. This is
# the "compact during migration" edge case — without this, the next hook
# tick would not realize it needs to rebootstrap.
if recall::is_fresh_install; then
  rm -f "${RECALL_DATA_ROOT}/.bootstrap_banner_shown" 2>/dev/null || true
  recall::log "post-compact-recovery: detected fresh index, cleared bootstrap banner marker"
fi

recall::log "post-compact-recovery: post_compact=true session=$STATE_KEY wrote=$WROTE"
recall::log_json hook.post_compact_recovery elapsed_ms="$(recall::elapsed_ms)" cwd="$CWD" session_id="$SESSION_ID" state_file="$STATE_FILE" wrote="$WROTE"
exit 0
