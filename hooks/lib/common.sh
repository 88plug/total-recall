#!/usr/bin/env bash
# Shared helpers for total-recall hooks.
# Source from each hook script. Caller is responsible for `set -euo pipefail`.

# Resolve plugin-data root. CLAUDE_PLUGIN_DATA is set by the harness when the
# plugin runs; fall back to a sane default for ad-hoc runs and tests.
RECALL_DATA_ROOT="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/plugins/data}/total-recall"
RECALL_LOG_DIR="${RECALL_DATA_ROOT}/logs"
RECALL_LOG_FILE="${RECALL_LOG_DIR}/hooks.log"
RECALL_LOG_MAX_BYTES="${RECALL_LOG_MAX_BYTES:-1048576}"  # 1 MiB

# Read JSON from stdin once; expose as $RECALL_INPUT. Survives `set -u`.
recall::read_input() {
  if [ -t 0 ]; then
    RECALL_INPUT=""
  else
    RECALL_INPUT="$(cat -)"
  fi
  export RECALL_INPUT
}

# Pull a top-level field from the hook input JSON. Returns empty on miss.
# Usage: recall::field session_id   →  prints value or nothing
recall::field() {
  local key="${1:-}"
  [ -n "$key" ] || { printf ''; return 0; }
  command -v jq >/dev/null 2>&1 || { printf ''; return 0; }
  printf '%s' "${RECALL_INPUT:-}" \
    | jq -r --arg k "$key" '.[$k] // empty' 2>/dev/null \
    || printf ''
}

# Slug a cwd the same way Claude Code's projects/ dir does (every `/` → `-`).
# Note: this is a coarse mirror; the canonical slug in Claude Code replaces
# any non-alnum with `-`, but for our log/key uses this is sufficient.
recall::cwd_slug() {
  local cwd="${1:-${PWD:-/unknown}}"
  printf '%s' "${cwd//\//-}"
}

# Append a line to the hook log. Truncates (keeps tail) when over RECALL_LOG_MAX_BYTES.
recall::log() {
  mkdir -p "$RECALL_LOG_DIR" 2>/dev/null || return 0
  if [ -f "$RECALL_LOG_FILE" ]; then
    local size
    size="$(stat -c %s "$RECALL_LOG_FILE" 2>/dev/null \
            || stat -f %z "$RECALL_LOG_FILE" 2>/dev/null \
            || echo 0)"
    if [ "${size:-0}" -gt "$RECALL_LOG_MAX_BYTES" ] 2>/dev/null; then
      # Keep last ~half of the file so we never lose recent context.
      local tmp="${RECALL_LOG_FILE}.tmp.$$"
      tail -c $(( RECALL_LOG_MAX_BYTES / 2 )) "$RECALL_LOG_FILE" > "$tmp" 2>/dev/null \
        && mv "$tmp" "$RECALL_LOG_FILE" 2>/dev/null \
        || rm -f "$tmp" 2>/dev/null
    fi
  fi
  printf '%s %s\n' "$(date -Iseconds 2>/dev/null || date)" "$*" \
    >> "$RECALL_LOG_FILE" 2>/dev/null || true
}

# Emit the standard hookSpecificOutput envelope. args: context_text, event_name.
recall::emit_context() {
  local ctx="${1:-}"
  local evt="${2:-}"
  [ -n "$ctx" ] || return 0
  [ -n "$evt" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  jq -n --arg c "$ctx" --arg evt "$evt" \
    '{hookSpecificOutput:{hookEventName:$evt,additionalContext:$c}}'
}

# recall::log_json <event_name> [key=value ...]
# Emits one NDJSON line to ${CLAUDE_PLUGIN_DATA}/total-recall/logs/events.jsonl
# (the same file total_recall.events writes to from Python — MA4).
# Best-effort: any failure is silently swallowed.
recall::log_json() {
  local event="$1"; shift
  local logdir="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data}/total-recall/logs"
  local logfile="$logdir/events.jsonl"
  mkdir -p "$logdir" 2>/dev/null || return 0
  local ts; ts=$(date -u -Iseconds 2>/dev/null || date -u +%FT%TZ)
  if command -v jq >/dev/null 2>&1; then
    local args=(--arg ts "$ts" --arg event "$event")
    local jq_body='{ts:$ts,event:$event'
    local kv k v
    for kv in "$@"; do
      k="${kv%%=*}"; v="${kv#*=}"
      args+=(--arg "$k" "$v")
      jq_body+=",${k}:\$${k}"
    done
    jq_body+='}'
    jq -nc "${args[@]}" "$jq_body" >> "$logfile" 2>/dev/null || true
  else
    local pairs="" kv
    for kv in "$@"; do
      pairs+=",\"${kv%%=*}\":\"${kv#*=}\""
    done
    printf '{"ts":"%s","event":"%s"%s}\n' "$ts" "$event" "$pairs" >> "$logfile" 2>/dev/null || true
  fi
}

# Capture a high-resolution start timestamp. Use $EPOCHREALTIME on bash 5+,
# fall back to `date +%s.%N` otherwise. Stored as RECALL_START_TS.
recall::start_timer() {
  if [ -n "${EPOCHREALTIME:-}" ]; then
    RECALL_START_TS="$EPOCHREALTIME"
  else
    RECALL_START_TS="$(date +%s.%N 2>/dev/null || date +%s)"
  fi
  export RECALL_START_TS
}

# Compute elapsed ms since recall::start_timer. Prints integer milliseconds.
recall::elapsed_ms() {
  local now
  if [ -n "${EPOCHREALTIME:-}" ]; then
    now="$EPOCHREALTIME"
  else
    now="$(date +%s.%N 2>/dev/null || date +%s)"
  fi
  awk -v a="$now" -v b="${RECALL_START_TS:-0}" 'BEGIN{print int((a-b)*1000)}' 2>/dev/null || printf 0
}

recall::has_jq() { command -v jq >/dev/null 2>&1; }
recall::has_py() { command -v python3 >/dev/null 2>&1; }

# === uv-backed runner (v0.7.0) ================================================
# Switched from "find or build a python venv" (v0.6.2) to "run via uv".
# Rationale: v0.6.2 still required python>=3.10 on PATH. On Ubuntu 18.04 LTS
# the system python is 3.6, and we couldn't expect every user to apt-install
# a newer python or know how. uv is a 25 MB static Rust binary that brings
# its own python — so the plugin's only host prereq is now bash+curl+internet.
#
# Resolution order:
#   1. $RECALL_UV env override
#   2. uv on PATH (system install)
#   3. $PLUGIN_DATA/bin/uv (we bootstrapped it on a prior fire)
#   4. download uv into $PLUGIN_DATA/bin/uv via the official installer
#      (one-time, ~3-5s for the binary; first `uv run` then takes ~30s to
#      download python 3.12 + resolve deps; subsequent runs are <1s)
#
# Prints the chosen uv path to stdout; returns nonzero on hard failure.

recall::_plugin_root() {
  # CLAUDE_PLUGIN_ROOT is injected by the harness for hooks. Fall back to
  # walking up from this file's location when run ad-hoc.
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    printf '%s' "$CLAUDE_PLUGIN_ROOT"
  else
    # common.sh lives at $PLUGIN_ROOT/hooks/lib/common.sh
    local d; d="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
    printf '%s' "$d"
  fi
}

recall::_can_run_uv() {
  local uv="$1"
  [ -x "$uv" ] && "$uv" --version >/dev/null 2>&1
}

# Download the uv binary into $PLUGIN_DATA/bin/uv. Uses the official static
# installer if curl is present. Returns 0 on success.
#
# We install into the plugin's persistent data dir so the binary survives
# plugin updates (CLAUDE_PLUGIN_ROOT is wiped on update; CLAUDE_PLUGIN_DATA
# is not). UV_INSTALL_DIR controls where the installer drops the binary.
recall::_install_uv() {
  local dest_bin="${RECALL_DATA_ROOT}/bin"
  mkdir -p "$dest_bin" 2>/dev/null || { recall::log "install_uv: can't create $dest_bin"; return 1; }
  if ! command -v curl >/dev/null 2>&1; then
    recall::log "install_uv: curl not on PATH — install curl, or install uv manually (https://docs.astral.sh/uv/)"
    return 1
  fi
  recall::log "install_uv: downloading uv to $dest_bin (one-time, ~3-5s)"
  # The Astral installer writes uv + uvx into $UV_INSTALL_DIR.
  # --no-modify-path so we don't try to edit the user's shell rc files.
  if ! UV_INSTALL_DIR="$dest_bin" \
       UV_UNMANAGED_INSTALL=1 \
       sh -c "curl -LsSf https://astral.sh/uv/install.sh | sh" \
       >>"$RECALL_LOG_FILE" 2>&1; then
    recall::log "install_uv: installer failed; see hooks.log"
    return 1
  fi
  if [ ! -x "$dest_bin/uv" ]; then
    recall::log "install_uv: installer ran but $dest_bin/uv is missing"
    return 1
  fi
  recall::log "install_uv: uv ready at $dest_bin/uv ($("$dest_bin/uv" --version 2>&1))"
  return 0
}

# Cached result so multiple calls within one hook don't rerun the probe.
RECALL_UV_CACHED=""

recall::uv() {
  if [ -n "$RECALL_UV_CACHED" ]; then
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  # 1. Explicit env override.
  if [ -n "${RECALL_UV:-}" ] && recall::_can_run_uv "$RECALL_UV"; then
    RECALL_UV_CACHED="$RECALL_UV"
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  # 2. uv on PATH.
  if command -v uv >/dev/null 2>&1 && recall::_can_run_uv "$(command -v uv)"; then
    RECALL_UV_CACHED="$(command -v uv)"
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  # 3. Previously-bootstrapped uv under plugin data root.
  local bundled="${RECALL_DATA_ROOT}/bin/uv"
  if recall::_can_run_uv "$bundled"; then
    RECALL_UV_CACHED="$bundled"
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  # 4. Install uv on demand (one-time, then cached for the lifetime of the
  # plugin's data dir — survives plugin updates).
  if recall::_install_uv && recall::_can_run_uv "$bundled"; then
    RECALL_UV_CACHED="$bundled"
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  return 1
}

# Convenience: print the resolved uv AND fail-fast for hooks that need it.
recall::require_uv() {
  local u
  if u="$(recall::uv)"; then
    printf '%s' "$u"
    return 0
  fi
  recall::log "require_uv: no uv available and autoinstall failed; skipping"
  return 1
}

# Build the canonical uv-run argv prefix that all hooks use to invoke
# total_recall code. Usage:
#   "$RECALL_UV" $(recall::uv_run_args) python -m total_recall index ...
#
# Pinned to --python ">=3.10" so uv installs/reuses a managed python that
# satisfies the plugin's requires-python; pinned to --project so uv uses the
# plugin's own pyproject.toml for dependency resolution and caches the venv
# inside $PLUGIN_ROOT/.venv (idempotent, fast after first run).
recall::uv_run_args() {
  local root; root="$(recall::_plugin_root)"
  printf 'run --project %q --python >=3.10' "$root"
}

# Run total_recall code via uv. Wraps the verbose recall::uv_run_args incantation.
# Usage:
#   recall::run -m total_recall index --since-last-tick
#   recall::run -c "$PY_SNIPPET" -- "$CWD"
#   recall::run "$HOOK_DIR/lib/decide_and_format.py" --arg ...
#   echo "..." | recall::run - <<'PY'
#                ...
#                PY
recall::run() {
  local uv; uv="$(recall::uv)" || return 1
  local root; root="$(recall::_plugin_root)"
  "$uv" run --project "$root" --python ">=3.10" python "$@"
}

# Back-compat aliases: hooks shipped against the v0.6.2 API expected $RECALL_PY
# to point at a python interpreter. Under v0.7.0 we no longer have a single
# python path — every invocation goes through `uv run`. Provide aliases that
# resolve uv and emit a tiny shim wrapper so the old call shape keeps working
# for in-flight hooks across the version-bump boundary.
#
# Deprecated: use recall::run instead.
RECALL_PY_CACHED=""
recall::python() {
  # Returns a path to a wrapper script that exec's `uv run ... python "$@"`.
  # We materialize it lazily under the plugin data dir.
  if [ -n "$RECALL_PY_CACHED" ]; then
    printf '%s' "$RECALL_PY_CACHED"
    return 0
  fi
  local uv; uv="$(recall::uv)" || return 1
  local root; root="$(recall::_plugin_root)"
  local shim="${RECALL_DATA_ROOT}/bin/uv-python-shim"
  mkdir -p "$(dirname "$shim")" 2>/dev/null || return 1
  if [ ! -x "$shim" ]; then
    cat > "$shim" <<EOF
#!/usr/bin/env bash
exec "$uv" run --project "$root" --python ">=3.10" python "\$@"
EOF
    chmod +x "$shim"
  fi
  RECALL_PY_CACHED="$shim"
  printf '%s' "$RECALL_PY_CACHED"
}

recall::require_python() {
  local p
  if p="$(recall::python)"; then
    printf '%s' "$p"
    return 0
  fi
  recall::log "require_python: uv unavailable and autoinstall failed; skipping"
  return 1
}

# Fresh-install / bootstrap helpers.
#
# When a user enables total-recall mid-session, no DB exists yet. The first
# hook to fire detects this and kicks off a FULL backfill of ~/.claude/projects/
# detached in the background — so the hook itself returns immediately and the
# user's session is never blocked. A lockfile (.bootstrapping) flags the work
# is in progress; other hooks check for it and either skip duplicate launches
# or emit a status envelope to the user.

RECALL_BOOTSTRAP_LOCK="${RECALL_DATA_ROOT}/.bootstrapping"
RECALL_BOOTSTRAP_LOG="${RECALL_LOG_DIR}/bootstrap.log"
# DB is considered "fresh" if missing or smaller than ~100KB (empty schema is
# only a few KB; one real session inflates it past 100KB immediately).
RECALL_FRESH_SIZE_THRESHOLD="${RECALL_FRESH_SIZE_THRESHOLD:-102400}"

# Returns 0 if the index DB looks fresh/empty, 1 otherwise.
recall::is_fresh_install() {
  local db="${RECALL_DATA_ROOT}/index.db"
  if [ ! -f "$db" ]; then
    return 0
  fi
  local size
  size="$(stat -c %s "$db" 2>/dev/null || stat -f %z "$db" 2>/dev/null || echo 0)"
  [ "${size:-0}" -lt "$RECALL_FRESH_SIZE_THRESHOLD" ] 2>/dev/null
}

# Returns 0 if a bootstrap is currently in progress (lockfile exists AND its
# recorded PID is still alive AND it's <30min old — stale locks are ignored).
recall::bootstrap_in_progress() {
  [ -f "$RECALL_BOOTSTRAP_LOCK" ] || return 1
  local lock_age now mtime
  now="$(date +%s 2>/dev/null || echo 0)"
  mtime="$(stat -c %Y "$RECALL_BOOTSTRAP_LOCK" 2>/dev/null \
           || stat -f %m "$RECALL_BOOTSTRAP_LOCK" 2>/dev/null \
           || echo 0)"
  lock_age=$(( now - mtime ))
  if [ "$lock_age" -gt 1800 ] 2>/dev/null; then
    # Stale (>30min); treat as not-in-progress so a new bootstrap can run.
    rm -f "$RECALL_BOOTSTRAP_LOCK" 2>/dev/null
    return 1
  fi
  local pid
  pid="$(awk 'NR==1{print $1}' "$RECALL_BOOTSTRAP_LOCK" 2>/dev/null || echo)"
  if [ -z "$pid" ]; then
    return 0  # lockfile exists but malformed; assume in progress
  fi
  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  # PID is dead — bootstrap crashed or finished without cleanup. Treat as done.
  rm -f "$RECALL_BOOTSTRAP_LOCK" 2>/dev/null
  return 1
}

# Detach a full backfill in the background. Writes a lockfile so concurrent
# hooks don't double-launch. Returns immediately. Never blocks.
recall::start_bootstrap() {
  if recall::bootstrap_in_progress; then
    return 0
  fi
  local uv
  uv="$(recall::uv)" || { recall::log "bootstrap skipped: uv unavailable and autoinstall failed"; return 0; }
  local root; root="$(recall::_plugin_root)"
  mkdir -p "$RECALL_DATA_ROOT" "$RECALL_LOG_DIR" 2>/dev/null || return 0

  # setsid detaches from the controlling terminal AND escapes the process
  # group, so Claude Code's hook timeout can't kill the backfill. nohup is
  # belt-and-suspenders for shells where setsid is unavailable.
  local launcher
  if command -v setsid >/dev/null 2>&1; then
    launcher="setsid nohup"
  else
    launcher="nohup"
  fi

  # We invoke the CLI module instead of importing inline because the CLI's
  # signal handlers + structured logging are already wired.
  #
  # --jobs N parallelizes JSONL parsing across N workers (DB writes stay
  # serialized — SQLite has one writer at a time). Cap at 8 to keep memory
  # pressure manageable on small VPSes; on a 12-core dev box this still gets
  # us 5-8x speedup vs single-threaded for a multi-hundred-MB corpus.
  local recall_jobs
  recall_jobs="$(nproc 2>/dev/null || echo 4)"
  if [ "$recall_jobs" -gt 8 ] 2>/dev/null; then recall_jobs=8; fi
  $launcher "$uv" run --project "$root" --python ">=3.10" \
      python -m total_recall index --full --jobs "$recall_jobs" \
      > "$RECALL_BOOTSTRAP_LOG" 2>&1 < /dev/null &
  local pid=$!
  disown 2>/dev/null || true

  # Lockfile holds PID + start ISO + invoking hook event.
  printf '%s %s %s\n' "$pid" "$(date -Iseconds 2>/dev/null || date)" "${1:-?}" \
    > "$RECALL_BOOTSTRAP_LOCK" 2>/dev/null || true

  recall::log "bootstrap started: pid=$pid hook=${1:-?}"
  recall::log_json total_recall.bootstrap.start pid="$pid" hook="${1:-?}"
}

# Standard banner-style envelope text for the "bootstrap in progress" case.
recall::bootstrap_banner() {
  local hook_evt="${1:-SessionStart}"
  cat <<'EOF'
**[total-recall]** First-run indexing of ~/.claude/projects/ is happening in the background.

Past-session memory will be available once it finishes (typically 15–90 seconds for a few GB of transcripts). Run `/total-recall:recall-health` to check progress.

This message will not repeat — recall results will start surfacing automatically.
EOF
}
