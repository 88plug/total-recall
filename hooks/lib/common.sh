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

# Extract the uv binary BUNDLED with the plugin (vendor/uv/uv-<target>.tar.gz) for
# the current platform into the data dir, and echo its path. Lets first run work
# fully OFFLINE on any supported machine. Returns nonzero if no matching bundle.
recall::_uv_from_bundle() {
  local root vend os arch libc target tb dest
  root="$(recall::_plugin_root)"; vend="${root}/vendor/uv"
  [ -d "$vend" ] || return 1
  os="$(uname -s 2>/dev/null)"; arch="$(uname -m 2>/dev/null)"
  case "$os" in
    Linux)
      case "$arch" in x86_64|amd64) arch=x86_64 ;; aarch64|arm64) arch=aarch64 ;; *) return 1 ;; esac
      libc=gnu
      if (ldd --version 2>&1 | grep -qi musl) || ls /lib/ld-musl-* >/dev/null 2>&1; then libc=musl; fi
      target="${arch}-unknown-linux-${libc}" ;;
    Darwin)
      case "$arch" in x86_64) arch=x86_64 ;; arm64|aarch64) arch=aarch64 ;; *) return 1 ;; esac
      target="${arch}-apple-darwin" ;;
    *) return 1 ;;
  esac
  tb="${vend}/uv-${target}.tar.gz"
  [ -f "$tb" ] || return 1
  dest="${RECALL_DATA_ROOT}/bin"; mkdir -p "$dest" || return 1
  tar xzf "$tb" --strip-components=1 -C "$dest" >/dev/null 2>&1 || return 1
  chmod +x "$dest/uv" "$dest/uvx" 2>/dev/null || true
  recall::_can_run_uv "$dest/uv" && { printf '%s' "$dest/uv"; return 0; }
  return 1
}

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

  # 3a. uv installed under the common user-level location but off the spawn PATH
  # (e.g. ~/.local/bin not on Claude Code's MCP-spawn PATH — the wildnuc case).
  if recall::_can_run_uv "${HOME}/.local/bin/uv"; then
    RECALL_UV_CACHED="${HOME}/.local/bin/uv"
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  # 3b. uv BUNDLED with the plugin (vendor/uv) — extract the matching platform's
  # binary. Offline-capable; preferred over a network download.
  local from_bundle
  if from_bundle="$(recall::_uv_from_bundle)"; then
    RECALL_UV_CACHED="$from_bundle"
    printf '%s' "$RECALL_UV_CACHED"
    return 0
  fi

  # 4. Install uv on demand (one-time, then cached for the lifetime of the
  # plugin's data dir — survives plugin updates). Last resort: needs network.
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

  # Fire LLM provision as a separate, independently-failable sidecar so it
  # can never slow or crash the transcript backfill above.
  recall::start_llm_provision
}

# Detach an incremental ingest tick (Stop / PostCompact). Returns immediately.
#
# Replaces the old foreground `timeout 55s ... index --since-last-tick`: a scan
# that ran past 55s was SIGTERM'd mid-write, so the ingest transaction never
# committed and ingest_state never advanced — the next tick re-scanned the same
# (growing) backlog and timed out again, a death spiral that pinned the index
# far below the on-disk corpus. Detaching (setsid+nohup, exactly like
# recall::start_bootstrap) removes the race entirely; an flock inside the child
# collapses overlapping ticks to one instead of letting them pile up.
#
#   $1 = resolved python invocation (the recall::python shim)
#   $2 = invoking hook event label (logs only)
#
# PYTHONPATH is exported by the caller, so the detached child inherits it.
recall::start_incremental_index() {
  local py="${1:-}"
  local evt="${2:-?}"
  [ -n "$py" ] || { recall::log "start_incremental_index: no python; skipping"; return 1; }

  mkdir -p "$RECALL_DATA_ROOT" 2>/dev/null || true
  local lock="${RECALL_DATA_ROOT}/.incremental.lock"

  local launcher="nohup"
  if command -v setsid >/dev/null 2>&1; then launcher="setsid nohup"; fi

  # RECALL_INCR_PY is passed through the environment so we avoid nested quoting
  # of the shim path inside the single-quoted child script.
  RECALL_INCR_PY="$py" $launcher bash -c '
    if command -v flock >/dev/null 2>&1; then
      exec 9>"$1" 2>/dev/null || exit 0
      flock -n 9 || exit 0   # another tick is already ingesting; let it finish
    fi
    "$RECALL_INCR_PY" -m total_recall index --since-last-tick >/dev/null 2>&1
  ' tr-incr "$lock" >/dev/null 2>&1 < /dev/null &
  disown 2>/dev/null || true
  recall::log "start_incremental_index: detached tick (hook=${evt}, lock=${lock})"
}

# === ollama resolver + LLM provision (v0.9.1) =================================
# Mirrors the recall::uv pattern exactly: resolve, then auto-fetch if absent.
# Every function is safe to call repeatedly (idempotent). Any failure logs +
# returns non-zero; callers treat non-zero as "LLM unavailable" and continue.

RECALL_OLLAMA_CACHED=""

recall::_can_run_ollama() {
  local bin="$1"
  [ -x "$bin" ] && "$bin" --version >/dev/null 2>&1
}

# True (0) iff an NVIDIA GPU is visible on this host. Cheap, cached. Used to
# decide whether GPU preference is even worth evaluating — on a CPU-only host
# the whole GPU pass is skipped and resolution is identical to before.
RECALL_HAS_NVIDIA_GPU=""   # "", "1" (yes), "0" (no)
recall::_has_nvidia_gpu() {
  if [ -n "$RECALL_HAS_NVIDIA_GPU" ]; then
    [ "$RECALL_HAS_NVIDIA_GPU" = "1" ]
    return
  fi
  if command -v nvidia-smi >/dev/null 2>&1 \
     && nvidia-smi -L >/dev/null 2>&1; then
    RECALL_HAS_NVIDIA_GPU=1
  elif ls /dev/nvidia[0-9]* >/dev/null 2>&1; then
    # nvidia-smi can be absent in containers that still pass through /dev/nvidia*.
    RECALL_HAS_NVIDIA_GPU=1
  else
    RECALL_HAS_NVIDIA_GPU=0
  fi
  [ "$RECALL_HAS_NVIDIA_GPU" = "1" ]
}

# Decide whether a given ollama binary can use the GPU. Returns 0 = GPU-capable,
# 1 = CPU-only / unknown. Two tiers, cheapest first; result is memoized per-bin.
#
#   Tier 1 (static, no daemon): does the binary — or the CUDA runtime libs it
#     dlopen's from its sibling lib/ollama/ dir — link/ship CUDA? ollama loads
#     CUDA at runtime, so `ldd` on the launcher alone under-reports; we also
#     glob the lib dir. This is the decisive signal for the bundled/fetched
#     binary (which ships libggml-cuda) vs a distro CPU-only build.
#   Tier 2 (live, only if a daemon is ALREADY reachable): GET /api/ps and check
#     whether any loaded model reports size_vram>0 (the field behind the
#     "100% GPU" column of `ollama ps`). We never start a daemon just to probe.
# True (0) iff the given binary is the executable backing the currently-running
# `ollama serve` process. Used to gate the live /api/ps GPU probe so a non-serving
# binary is never credited with another binary's GPU status. Best-effort: if we
# can't determine the serving exe, returns false (so Tier 2 is simply skipped).
recall::_ollama_is_serving_bin() {
  local bin="$1" want pid exe
  want="$(readlink -f "$bin" 2>/dev/null || printf '%s' "$bin")"
  command -v pgrep >/dev/null 2>&1 || return 1
  # `ollama serve` is the daemon; there may be short-lived `ollama run` procs too.
  for pid in $(pgrep -x ollama 2>/dev/null); do
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null)" || continue
    [ "$exe" = "$want" ] && return 0
  done
  return 1
}

declare -A RECALL_OLLAMA_GPU_CACHE 2>/dev/null || true
recall::_ollama_gpu_capable() {
  local bin="$1"
  [ -n "$bin" ] || return 1

  if [ -n "${RECALL_OLLAMA_GPU_CACHE[$bin]:-}" ]; then
    [ "${RECALL_OLLAMA_GPU_CACHE[$bin]}" = "1" ]
    return
  fi

  local verdict=0   # 0 = CPU-only until proven otherwise

  # --- Tier 1: static link / shipped-lib inspection (no daemon needed) -------
  if command -v ldd >/dev/null 2>&1 \
     && ldd "$bin" 2>/dev/null | grep -qiE 'libcud(a|art)|libcublas|cuda'; then
    verdict=1
  else
    # ollama dlopen's its compute backend from lib/ollama/ next to the binary
    # (real path, so resolve symlinks first). The official tarball nests the
    # CUDA backend one level deeper, in lib/ollama/cuda_v12|cuda_v13/, so we
    # search recursively for libggml-cuda / libcudart across the likely roots.
    # Roots are resolved RELATIVE to this binary's own install prefix only —
    # never a global /usr path — so testing some unrelated binary can't be
    # credited with a system ollama's CUDA libs. Layouts: prefix/bin/ollama with
    # prefix/lib/ollama/... (tarball), or .../bin/ollama with sibling lib/ollama.
    local real bindir root
    real="$(readlink -f "$bin" 2>/dev/null || printf '%s' "$bin")"
    bindir="$(dirname "$real")"
    for root in "${bindir}/../lib/ollama" "${bindir}/lib/ollama"; do
      [ -d "$root" ] || continue
      if find "$root" \( -name 'libggml-cuda*' -o -name 'libcudart*' \) \
              -print -quit 2>/dev/null | grep -q .; then
        verdict=1
        break
      fi
    done
  fi

  # --- Tier 2: ask a daemon that is ALREADY running (only if Tier 1 unsure) --
  # Guard: only attribute a daemon's GPU status to THIS binary if this binary
  # is the one actually serving — otherwise a CPU build gets falsely credited
  # for a GPU daemon that some other binary started. We match the candidate's
  # resolved path against the running `ollama serve` process's executable.
  if [ "$verdict" -eq 0 ] && command -v curl >/dev/null 2>&1 \
     && recall::_ollama_is_serving_bin "$bin"; then
    local base_url="${TOTAL_RECALL_LLM_BASE_URL:-http://localhost:11434}"
    local ps_json
    if ps_json="$(curl -sf --max-time 2 "${base_url}/api/ps" 2>/dev/null)" \
       && [ -n "$ps_json" ]; then
      if command -v jq >/dev/null 2>&1; then
        # Any loaded model with VRAM allocated ⇒ this daemon is on the GPU.
        if printf '%s' "$ps_json" \
           | jq -e '[.models[]? | select((.size_vram // 0) > 0)] | length > 0' \
           >/dev/null 2>&1; then
          verdict=1
        fi
      else
        # jq-less fallback: a nonzero size_vram anywhere in the payload.
        printf '%s' "$ps_json" | grep -qE '"size_vram"[[:space:]]*:[[:space:]]*[1-9]' && verdict=1
      fi
    fi
  fi

  RECALL_OLLAMA_GPU_CACHE[$bin]="$verdict"
  [ "$verdict" -eq 1 ]
}

# Detect CPU arch and map to the ollama tarball suffix (amd64 / arm64).
recall::_ollama_arch() {
  local m; m="$(uname -m 2>/dev/null || echo unknown)"
  case "$m" in
    x86_64)          printf 'amd64' ;;
    aarch64|arm64)   printf 'arm64' ;;
    *)               printf 'unsupported'; return 1 ;;
  esac
}

# Download the CPU-only ollama tarball into ${RECALL_DATA_ROOT}/bin/ — no sudo.
# The tarball layout is bin/ollama (+ optional lib/). We extract so the binary
# lands at ${RECALL_DATA_ROOT}/bin/ollama.
recall::_install_ollama() {
  local dest_bin="${RECALL_DATA_ROOT}/bin"
  mkdir -p "$dest_bin" 2>/dev/null || { recall::log "install_ollama: can't create $dest_bin"; return 1; }
  if ! command -v curl >/dev/null 2>&1; then
    recall::log "install_ollama: curl not on PATH — install curl, or install ollama manually (https://ollama.com)"
    return 1
  fi
  local arch; arch="$(recall::_ollama_arch)" || {
    recall::log "install_ollama: unsupported arch $(uname -m) — install ollama manually"
    return 1
  }
  local url="https://ollama.com/download/ollama-linux-${arch}.tgz"
  local tmp; tmp="$(mktemp --suffix=.tgz 2>/dev/null || mktemp /tmp/ollama.tgz.XXXXXX)"
  trap 'rm -f "$tmp"' RETURN
  recall::log "install_ollama: downloading ${url} (~38MB CPU tarball, no sudo needed)"
  if ! curl -fsSL --retry 3 -o "$tmp" "$url" 2>>"$RECALL_LOG_FILE"; then
    recall::log "install_ollama: download failed; see hooks.log"
    return 1
  fi
  # The tarball contains bin/ollama (relative path); extract into dest_bin
  # parent so it becomes dest_bin/ollama.
  local extract_root; extract_root="$(dirname "$dest_bin")"
  if ! tar -xzf "$tmp" -C "$extract_root" 2>>"$RECALL_LOG_FILE"; then
    recall::log "install_ollama: tar extract failed; see hooks.log"
    return 1
  fi
  if [ ! -x "$dest_bin/ollama" ]; then
    # Some releases ship as a flat binary rather than bin/ollama inside the tar.
    # If the binary landed directly in extract_root, move it.
    if [ -x "$extract_root/ollama" ]; then
      mv "$extract_root/ollama" "$dest_bin/ollama"
    else
      recall::log "install_ollama: extracted but $dest_bin/ollama not found"
      return 1
    fi
  fi
  recall::log "install_ollama: ollama ready at $dest_bin/ollama ($("$dest_bin/ollama" --version 2>&1 || echo '?'))"
  return 0
}

# Resolve a usable ollama binary. Candidate order (highest priority first):
#   1. $RECALL_OLLAMA env override
#   2. ${RECALL_DATA_ROOT}/bin/ollama (our managed copy — predictable version/GPU)
#   3. ollama on PATH (system install — fallback only)
#   4. /snap/bin/ollama (Ubuntu snap install)
#   5. auto-fetch (downloads the official tarball, no sudo — ships CUDA libs)
#
# GPU preference (RECALL_PREFER_GPU=1, the default): when an NVIDIA GPU is
# present we make TWO passes over the candidates — pass 1 returns the first
# GPU-capable candidate, pass 2 falls back to the first runnable one. This
# stops a distro CPU-only build (e.g. Arch's /usr/bin/ollama, no CUDA linked)
# from shadowing the CUDA-capable bundled/fetched binary. On a CPU-only host,
# or with RECALL_PREFER_GPU=0, this collapses to the original first-runnable
# behavior with zero extra work.
#
# Prints the resolved path to stdout; returns non-zero if none found/fetchable.
recall::ollama() {
  if [ -n "$RECALL_OLLAMA_CACHED" ]; then
    printf '%s' "$RECALL_OLLAMA_CACHED"
    return 0
  fi

  local prefer_gpu="${RECALL_PREFER_GPU:-1}"
  local bundled="${RECALL_DATA_ROOT}/bin/ollama"

  # Build the candidate list in priority order. Auto-fetch is handled inline
  # below (it has a side effect) rather than pre-listed.
  local -a candidates=()
  [ -n "${RECALL_OLLAMA:-}" ] && candidates+=("$RECALL_OLLAMA")
  # Bundled binary takes priority over whatever the user has installed — we own
  # and manage this copy so its version and GPU support are predictable.
  candidates+=("$bundled")
  command -v ollama >/dev/null 2>&1 && candidates+=("$(command -v ollama)")
  candidates+=("/snap/bin/ollama")

  local want_gpu=0
  if [ "$prefer_gpu" = "1" ] && recall::_has_nvidia_gpu; then
    want_gpu=1
  fi

  local cand

  # RECALL_OLLAMA explicit override bypasses GPU checking — operator said
  # exactly which binary to use, so respect it unconditionally.
  if [ -n "${RECALL_OLLAMA:-}" ] && recall::_can_run_ollama "$RECALL_OLLAMA"; then
    RECALL_OLLAMA_CACHED="$RECALL_OLLAMA"
    recall::log "ollama: using explicit RECALL_OLLAMA override $RECALL_OLLAMA"
    printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
  fi

  # Pass 1: GPU-capable candidate (only when we want GPU). We deliberately do
  # NOT auto-fetch here yet — we first see if any already-present binary is
  # GPU-capable before paying for a download.
  if [ "$want_gpu" -eq 1 ]; then
    for cand in "${candidates[@]}"; do
      [ "$cand" = "${RECALL_OLLAMA:-}" ] && continue  # already handled above
      if recall::_can_run_ollama "$cand" && recall::_ollama_gpu_capable "$cand"; then
        RECALL_OLLAMA_CACHED="$cand"
        recall::log "ollama: selected GPU-capable binary $cand"
        printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
      fi
    done

    # No present binary is GPU-capable. The official tarball ships CUDA libs,
    # so fetch it and prefer it over any CPU-only binary we found.
    if recall::_can_run_ollama "$bundled" || { recall::_install_ollama && recall::_can_run_ollama "$bundled"; }; then
      if recall::_ollama_gpu_capable "$bundled"; then
        RECALL_OLLAMA_CACHED="$bundled"
        recall::log "ollama: selected GPU-capable fetched binary $bundled (no CUDA binary was on PATH)"
        printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
      fi
    fi

    # Still nothing GPU-capable. Warn (do NOT kill any running CPU daemon) and
    # fall through to the CPU pass so refinement still works, just slower.
    local base_url="${TOTAL_RECALL_LLM_BASE_URL:-http://localhost:11434}"
    if curl -sf --max-time 2 "${base_url}/api/tags" >/dev/null 2>&1; then
      recall::log "ollama: NVIDIA GPU present but a CPU-only daemon is already serving ${base_url}; using CPU. To switch, stop that daemon and re-run — total-recall will not kill it."
    else
      recall::log "ollama: NVIDIA GPU present but no GPU-capable binary found; falling back to CPU-only."
    fi
  fi

  # Pass 2 (and the only pass when want_gpu=0): first runnable candidate.
  for cand in "${candidates[@]}"; do
    if recall::_can_run_ollama "$cand"; then
      RECALL_OLLAMA_CACHED="$cand"
      printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
    fi
  done

  # Auto-fetch as last resort (no GPU preference, or every present binary failed).
  if recall::_install_ollama && recall::_can_run_ollama "$bundled"; then
    RECALL_OLLAMA_CACHED="$bundled"
    printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
  fi

  return 1
}

# Start the ollama daemon if not already reachable. Detaches via setsid/nohup
# (same pattern as recall::start_bootstrap). Idempotent — no-op if port up.
# Logs to ${RECALL_LOG_DIR}/llm-provision.log.
recall::ollama_serve() {
  local base_url="${TOTAL_RECALL_LLM_BASE_URL:-http://localhost:11434}"
  if curl -sf "${base_url}/api/tags" >/dev/null 2>&1; then
    recall::log "ollama_serve: daemon already reachable at ${base_url}"
    return 0
  fi
  local ollama_bin; ollama_bin="$(recall::ollama)" || return 1
  local log_file="${RECALL_LOG_DIR}/llm-provision.log"
  mkdir -p "$RECALL_LOG_DIR" 2>/dev/null || true
  local launcher="nohup"
  if command -v setsid >/dev/null 2>&1; then launcher="setsid nohup"; fi
  recall::log "ollama_serve: starting daemon (log: $log_file)"
  $launcher "$ollama_bin" serve >>"$log_file" 2>&1 < /dev/null &
  local daemon_pid=$!
  disown 2>/dev/null || true
  # Wait up to 10s for the port to open.
  local i=0
  while [ $i -lt 10 ]; do
    i=$(( i + 1 ))
    sleep 1
    if curl -sf "${base_url}/api/tags" >/dev/null 2>&1; then
      recall::log "ollama_serve: daemon up (pid=$daemon_pid, waited ${i}s)"
      return 0
    fi
  done
  recall::log "ollama_serve: daemon still not reachable after 10s — continuing anyway"
  return 1
}

# Pull a model if it is not already present. Idempotent.
recall::ollama_pull() {
  local model="$1"
  local ollama_bin; ollama_bin="$(recall::ollama)" || return 1
  if "$ollama_bin" list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "${model}\(:latest\)\?"; then
    recall::log "ollama_pull: model ${model} already present"
    return 0
  fi
  recall::log "ollama_pull: pulling ${model} — may be several minutes / GB-scale download"
  local log_file="${RECALL_LOG_DIR}/llm-provision.log"
  "$ollama_bin" pull "${model}" >> "$log_file" 2>&1
}

# Orchestrator: respects opt-out, calls ollama→serve→pull, drops sentinel.
# Safe to call repeatedly — exits immediately if sentinel exists.
# NEVER fatal: every failure path returns 0 (LLM is optional).
recall::provision_llm() {
  local log_file="${RECALL_LOG_DIR}/llm-provision.log"
  mkdir -p "$RECALL_LOG_DIR" 2>/dev/null || true

  # Opt-out: TOTAL_RECALL_LLM_PROVIDER=none means "user doesn't want LLM".
  local provider="${TOTAL_RECALL_LLM_PROVIDER:-auto}"
  if [ "$provider" = "none" ]; then
    return 0
  fi

  local sentinel="${RECALL_DATA_ROOT}/.ollama_ready"
  if [ -f "$sentinel" ]; then
    return 0
  fi

  local model="${TOTAL_RECALL_LLM_MODEL:-qwen3.5:2b}"

  {
    printf '[llm-provision] %s starting (model=%s)\n' "$(date -Iseconds 2>/dev/null || date)" "$model"

    # Step 1: resolve (or fetch) ollama.
    local ollama_bin
    if ! ollama_bin="$(recall::ollama)"; then
      printf '[llm-provision] WARN: could not resolve or fetch ollama; LLM refinement disabled\n'
      return 0
    fi
    printf '[llm-provision] ollama binary: %s\n' "$ollama_bin"

    # Step 2: start daemon if needed.
    if ! recall::ollama_serve; then
      printf '[llm-provision] WARN: daemon start failed; LLM refinement disabled\n'
      return 0
    fi

    # Step 3: pull model if needed.
    if ! recall::ollama_pull "$model"; then
      printf '[llm-provision] WARN: model pull failed; LLM refinement disabled\n'
      return 0
    fi

    # Step 4: sentinel.
    touch "$sentinel" 2>/dev/null || true
    printf '[llm-provision] done — sentinel written: %s\n' "$sentinel"
  } >> "$log_file" 2>&1

  recall::log "provision_llm: complete (model=$model)"
  return 0
}

# Launch recall::provision_llm as a fully-detached sidecar process. Returns
# immediately — the LLM provision cannot slow or block the caller.
recall::start_llm_provision() {
  local provider="${TOTAL_RECALL_LLM_PROVIDER:-auto}"
  [ "$provider" = "none" ] && return 0

  local sentinel="${RECALL_DATA_ROOT}/.ollama_ready"
  [ -f "$sentinel" ] && return 0

  mkdir -p "$RECALL_DATA_ROOT" "$RECALL_LOG_DIR" 2>/dev/null || return 0

  local launcher="nohup"
  if command -v setsid >/dev/null 2>&1; then launcher="setsid nohup"; fi

  # Re-run this script (common.sh) is not executable — we need a small inline
  # bash wrapper that sources common.sh and calls provision_llm.
  local script_path="${BASH_SOURCE[0]}"
  $launcher bash -c "
    source $(printf '%q' "$script_path") 2>/dev/null || exit 0
    recall::provision_llm
  " > /dev/null 2>&1 < /dev/null &
  local sidecar_pid=$!
  disown 2>/dev/null || true
  recall::log "start_llm_provision: sidecar launched pid=$sidecar_pid"
}

# Return the model tag that get_default_client() will use.
# Resolution order: TOTAL_RECALL_LLM_MODEL env → CLI → hardcoded floor.
# Prints a single non-empty string; never fails or returns empty.
recall::llm_model() {
  if [ -n "${TOTAL_RECALL_LLM_MODEL:-}" ]; then
    printf '%s' "$TOTAL_RECALL_LLM_MODEL"
    return 0
  fi
  local uv root tag
  uv="$(recall::uv 2>/dev/null)" || uv=""
  root="$(recall::_plugin_root 2>/dev/null)" || root=""
  if [ -n "$uv" ] && [ -n "$root" ]; then
    tag="$("$uv" run --project "$root" --python ">=3.10" python -m total_recall llm-model 2>/dev/null || true)"
  fi
  if [ -n "${tag:-}" ]; then
    printf '%s' "$tag"
  else
    printf 'qwen3.5:2b'
  fi
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
