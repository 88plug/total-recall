#!/usr/bin/env bash
# Shared helpers for total-recall hooks.
# Source from each hook script. Caller is responsible for `set -euo pipefail`.

# Resolve plugin-data root. MUST match index.db.resolve_data_dir():
#   TOTAL_RECALL_DB_DIR → GROK_PLUGIN_DATA|CLAUDE_PLUGIN_DATA/total-recall
#   → largest existing plugin index under ~/.claude/plugins/data
#   → XDG ~/.local/share/total-recall
# Never invent a second DB when a plugin install already has one.
recall::data_root() {
  if [ -n "${TOTAL_RECALL_DB_DIR:-}" ]; then
    printf '%s' "${TOTAL_RECALL_DB_DIR}"
    return 0
  fi
  local pdata="${GROK_PLUGIN_DATA:-${CLAUDE_PLUGIN_DATA:-}}"
  if [ -n "$pdata" ]; then
    printf '%s' "${pdata%/}/total-recall"
    return 0
  fi
  local best="" best_sz=0 d sz
  # marketplace: …/data/total-recall-88plug/total-recall/index.db
  for d in "${HOME}/.claude/plugins/data"/total-recall*/total-recall; do
    [ -f "${d}/index.db" ] || continue
    sz=$(stat -c%s "${d}/index.db" 2>/dev/null || echo 0)
    if [ "${sz:-0}" -gt "${best_sz:-0}" ]; then
      best="$d"
      best_sz="$sz"
    fi
  done
  # legacy bare: …/data/total-recall/index.db
  d="${HOME}/.claude/plugins/data/total-recall"
  if [ -f "${d}/index.db" ]; then
    sz=$(stat -c%s "${d}/index.db" 2>/dev/null || echo 0)
    if [ "${sz:-0}" -gt "${best_sz:-0}" ]; then
      best="$d"
      best_sz="$sz"
    fi
  fi
  if [ -n "$best" ]; then
    printf '%s' "$best"
    return 0
  fi
  printf '%s' "${HOME}/.local/share/total-recall"
}
RECALL_DATA_ROOT="$(recall::data_root)"
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
# Emits one NDJSON line to <data-root>/logs/events.jsonl (the same file
# total_recall.events writes to from Python — MA4). Uses $RECALL_LOG_DIR so the
# stream always lands beside the index recall::data_root resolved; deriving a
# second path here would split events across two dirs whenever the harness has
# not exported CLAUDE_PLUGIN_DATA.
# Best-effort: any failure is silently swallowed.
recall::log_json() {
  local event="$1"; shift
  local logdir="${RECALL_LOG_DIR:-$(recall::data_root)/logs}"
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

# Product-owned HTTP endpoint. Never share system ollama's :11434 by default —
# foreign daemons on 11434 used to silently satisfy "port up" and freeze users
# on a stale system binary while the product client sat unused.
# Override only with TOTAL_RECALL_LLM_BASE_URL (operator pin).
RECALL_OLLAMA_DEFAULT_URL="http://127.0.0.1:11435"

recall::llm_base_url() {
  local u="${TOTAL_RECALL_LLM_BASE_URL:-}"
  if [ -z "$u" ] && [ -f "${RECALL_DATA_ROOT}/bin/.ollama-base-url" ]; then
    u="$(tr -d '[:space:]' <"${RECALL_DATA_ROOT}/bin/.ollama-base-url" 2>/dev/null || true)"
  fi
  if [ -z "$u" ]; then
    u="${RECALL_OLLAMA_DEFAULT_URL}"
  fi
  # strip trailing slash
  u="${u%/}"
  printf '%s' "$u"
}

# host:port for OLLAMA_HOST (ollama CLI + serve bind).
recall::llm_host_port() {
  local u host
  u="$(recall::llm_base_url)"
  u="${u#http://}"
  u="${u#https://}"
  host="${u%%/*}"
  printf '%s' "$host"
}

# True iff the product binary is the process serving *our* base URL.
# Reachable-but-foreign (system ollama, other users) → false.
#
# Candidate PIDs:
#   1. ${RECALL_DATA_ROOT}/bin/ollama.pid (written on product start)
#   2. pgrep -x ollama (real ELF product binary; never pgrep -f)
recall::_daemon_is_product() {
  local bin="${1:-}"
  local base_url host port pid exe want cmd environ pidfile candidates=""
  base_url="$(recall::llm_base_url)"
  curl -sf --max-time 2 "${base_url}/api/tags" >/dev/null 2>&1 || return 1

  if [ -z "$bin" ]; then
    bin="${RECALL_OLLAMA:-${RECALL_DATA_ROOT}/bin/ollama}"
  fi
  want="$(readlink -f "$bin" 2>/dev/null || printf '%s' "$bin")"
  [ -n "$want" ] || return 1
  host="$(recall::llm_host_port)"
  port="${host##*:}"
  [ "$port" = "$host" ] && port="11435"

  pidfile="${RECALL_DATA_ROOT}/bin/ollama.pid"
  if [ -f "$pidfile" ]; then
    candidates="$(tr -d '[:space:]' <"$pidfile" 2>/dev/null || true)"
  fi
  if command -v pgrep >/dev/null 2>&1; then
    candidates="${candidates} $(pgrep -x ollama 2>/dev/null || true)"
  fi

  for pid in $candidates; do
    [ -n "$pid" ] || continue
    [ -d "/proc/$pid" ] || continue
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    cmd="$(tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null || true)"
    printf '%s' "$cmd" | grep -q 'serve' || continue
    # Real ELF: /proc/pid/exe == product bin. Script wrapper: exe is the
    # interpreter — accept when cmdline contains the product bin path.
    if [ "$exe" != "$want" ] && ! printf '%s' "$cmd" | grep -Fq "$want"; then
      continue
    fi
    # Prefer OLLAMA_HOST match (we always set it on product start).
    environ="$(tr '\0' '\n' </proc/$pid/environ 2>/dev/null || true)"
    if printf '%s\n' "$environ" | grep -qx "OLLAMA_HOST=${host}" \
      || printf '%s\n' "$environ" | grep -qE "^OLLAMA_HOST=.+:${port}$"; then
      return 0
    fi
    # Fallback: process listens on our port (ss best-effort).
    if command -v ss >/dev/null 2>&1; then
      if ss -ltnp 2>/dev/null | grep -E ":${port}\\b" | grep -q "pid=${pid},"; then
        return 0
      fi
    fi
  done
  return 1
}

# Persist active product URL so bash + python + next hooks agree.
recall::_stamp_ollama_url() {
  local u="${1:-}"
  [ -n "$u" ] || return 0
  mkdir -p "${RECALL_DATA_ROOT}/bin" 2>/dev/null || true
  printf '%s\n' "$u" >"${RECALL_DATA_ROOT}/bin/.ollama-base-url" 2>/dev/null || true
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
    local base_url
    base_url="$(recall::llm_base_url)"
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

# Parse ``ollama --version`` → bare semver (e.g. 0.32.1). Empty on failure.
recall::_ollama_local_version() {
  local bin="${1:-}"
  [ -n "$bin" ] && [ -x "$bin" ] || return 1
  local raw v=""
  # Isolate from any running daemon. With a live older server, `ollama --version`
  # prints "ollama version is <server>" first and "client version is <bin>"
  # second — taking the first match poisons .ollama-latest with the server
  # version and blocks auto-update (seen 0.32.1 client + 0.30.10 server).
  raw="$(OLLAMA_HOST="${RECALL_OLLAMA_VERSION_PROBE_HOST:-127.0.0.1:0}" \
    "$bin" --version 2>&1 || true)"
  # Prefer explicit client line (always the binary).
  v="$(printf '%s' "$raw" | sed -nE 's/.*client version is ([0-9]+(\.[0-9]+)+).*/\1/p' | head -1)"
  if [ -z "$v" ]; then
    # Offline / single-line: "ollama version is 0.32.1" or "ollama version 0.32.1"
    v="$(printf '%s' "$raw" | sed -nE 's/.*version( is)? ([0-9]+(\.[0-9]+)+).*/\2/p' | head -1)"
  fi
  [ -n "$v" ] || return 1
  printf '%s' "$v"
}

# True if $1 is strictly older than $2 (semver-ish via sort -V).
recall::_version_lt() {
  local a="${1:-}" b="${2:-}"
  [ -n "$a" ] && [ -n "$b" ] || return 1
  [ "$a" = "$b" ] && return 1
  [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | head -1)" = "$a" ]
}

# Resolve latest ollama release tag (no leading v). Sources:
#   1. OLLAMA_VERSION / TOTAL_RECALL_OLLAMA_VERSION env pin
#   2. GitHub releases/latest (cached under data dir for TTL)
#   3. empty on total failure (caller keeps current binary)
recall::_ollama_latest_version() {
  local pin="${OLLAMA_VERSION:-${TOTAL_RECALL_OLLAMA_VERSION:-}}"
  pin="${pin#v}"
  if [ -n "$pin" ]; then
    printf '%s' "$pin"
    return 0
  fi
  local cache="${RECALL_DATA_ROOT}/bin/.ollama-latest"
  local ttl="${RECALL_OLLAMA_UPDATE_TTL_S:-86400}"
  local now age=999999999
  now="$(date +%s 2>/dev/null || echo 0)"
  if [ -f "$cache" ]; then
    local mtime
    mtime="$(stat -c %Y "$cache" 2>/dev/null || echo 0)"
    age=$((now - mtime))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$ttl" ]; then
      local cached
      cached="$(tr -d '[:space:]' <"$cache" 2>/dev/null || true)"
      if [ -n "$cached" ]; then
        printf '%s' "$cached"
        return 0
      fi
    fi
  fi
  command -v curl >/dev/null 2>&1 || return 1
  local tag=""
  # Prefer GitHub API (JSON); fall back to Location of /releases/latest.
  tag="$(curl -fsSL --max-time 8 \
    -H 'Accept: application/vnd.github+json' \
    'https://api.github.com/repos/ollama/ollama/releases/latest' 2>/dev/null \
    | sed -nE 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v?([^"]+)".*/\1/p' \
    | head -1)"
  if [ -z "$tag" ]; then
    tag="$(curl -fsSI --max-time 8 \
      'https://github.com/ollama/ollama/releases/latest' 2>/dev/null \
      | tr -d '\r' \
      | sed -nE 's|^[Ll]ocation: .*/tag/v?([^[:space:]]+).*|\1|p' \
      | head -1)"
  fi
  tag="${tag#v}"
  [ -n "$tag" ] || return 1
  mkdir -p "$(dirname "$cache")" 2>/dev/null || true
  printf '%s\n' "$tag" >"$cache" 2>/dev/null || true
  printf '%s' "$tag"
}

# Download product-managed ollama into ${RECALL_DATA_ROOT}/bin/ — no sudo.
#
# Official packaging (2026): primary Linux asset is ``.tar.zst`` (~1.4GB with
# CUDA libs). ``.tgz`` is gone for current releases (404 on v0.32.1) — keep as
# fallback for older pins only. Matches ollama.com/install.sh extract logic.
#
# Env:
#   OLLAMA_VERSION / TOTAL_RECALL_OLLAMA_VERSION — pin a release (e.g. 0.32.1)
#   RECALL_OLLAMA_AUTO_UPDATE=0 — install only when missing (no version bump)
recall::_install_ollama() {
  local dest_bin="${RECALL_DATA_ROOT}/bin"
  local extract_root="${RECALL_DATA_ROOT}"
  mkdir -p "$dest_bin" 2>/dev/null || { recall::log "install_ollama: can't create $dest_bin"; return 1; }
  if ! command -v curl >/dev/null 2>&1; then
    recall::log "install_ollama: curl not on PATH — install curl, or install ollama manually (https://ollama.com)"
    return 1
  fi
  local arch; arch="$(recall::_ollama_arch)" || {
    recall::log "install_ollama: unsupported arch $(uname -m) — install ollama manually"
    return 1
  }

  local pin="${OLLAMA_VERSION:-${TOTAL_RECALL_OLLAMA_VERSION:-}}"
  pin="${pin#v}"
  local ver_q=""
  [ -n "$pin" ] && ver_q="?version=${pin}"

  local base="https://ollama.com/download"
  local fname="ollama-linux-${arch}"
  local url="" fmt=""
  # Prefer zst (current releases). Probe with HEAD -L; fall back to tgz for pins.
  # Note: v0.32.1+ no longer ships .tgz for linux-amd64 (404) — zstd required.
  if curl -fsSIL --max-time 15 "${base}/${fname}.tar.zst${ver_q}" >/dev/null 2>&1; then
    url="${base}/${fname}.tar.zst${ver_q}"
    fmt="zst"
  elif curl -fsSIL --max-time 15 "${base}/${fname}.tgz${ver_q}" >/dev/null 2>&1; then
    url="${base}/${fname}.tgz${ver_q}"
    fmt="tgz"
  else
    # Unprobed direct GitHub latest zst as last resort.
    url="https://github.com/ollama/ollama/releases/latest/download/${fname}.tar.zst"
    fmt="zst"
  fi

  if [ "$fmt" = "zst" ] && ! command -v zstd >/dev/null 2>&1; then
    recall::log "install_ollama: zstd required for current ollama releases (apt install zstd / dnf install zstd)"
    return 1
  fi

  # Fresh extract dir per run — never wipe-and-reuse a variable path.
  local tmp_dir
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/tr-ollama.XXXXXX")" || {
    recall::log "install_ollama: mktemp failed"
    return 1
  }
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp_dir'" RETURN

  recall::log "install_ollama: downloading ${url} (product-managed, no sudo; ${fmt})"
  if [ "$fmt" = "zst" ]; then
    if ! curl -fsSL --retry 3 --max-time 600 "$url" 2>>"$RECALL_LOG_FILE" \
      | zstd -d 2>>"$RECALL_LOG_FILE" \
      | tar -xf - -C "$tmp_dir" 2>>"$RECALL_LOG_FILE"; then
      recall::log "install_ollama: zst download/extract failed; see hooks.log"
      return 1
    fi
  else
    if ! curl -fsSL --retry 3 --max-time 600 "$url" 2>>"$RECALL_LOG_FILE" \
      | tar -xzf - -C "$tmp_dir" 2>>"$RECALL_LOG_FILE"; then
      recall::log "install_ollama: tgz download/extract failed; see hooks.log"
      return 1
    fi
  fi

  # Locate binary in extract tree (bin/ollama or flat ollama).
  local src_bin=""
  if [ -x "$tmp_dir/bin/ollama" ]; then
    src_bin="$tmp_dir/bin/ollama"
  elif [ -x "$tmp_dir/ollama" ]; then
    src_bin="$tmp_dir/ollama"
  else
    src_bin="$(find "$tmp_dir" -type f -name ollama -perm -u+x 2>/dev/null | head -1 || true)"
  fi
  if [ -z "$src_bin" ] || [ ! -x "$src_bin" ]; then
    recall::log "install_ollama: extracted but ollama binary not found under $tmp_dir"
    return 1
  fi

  # If product binary is currently the serving daemon, stop it before replace.
  local dest="$dest_bin/ollama"
  if [ -x "$dest" ] && recall::_ollama_is_serving_bin "$dest"; then
    local pid
    for pid in $(pgrep -x ollama 2>/dev/null); do
      local exe
      exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
      local want
      want="$(readlink -f "$dest" 2>/dev/null || printf '%s' "$dest")"
      if [ "$exe" = "$want" ]; then
        recall::log "install_ollama: stopping product daemon pid=$pid for binary replace"
        kill "$pid" 2>/dev/null || true
      fi
    done
    sleep 0.5
  fi

  # Install binary + lib/ollama CUDA backends next to it (official layout).
  cp -f "$src_bin" "$dest" 2>>"$RECALL_LOG_FILE" || {
    recall::log "install_ollama: failed to copy binary to $dest"
    return 1
  }
  chmod +x "$dest" 2>/dev/null || true

  local src_lib=""
  if [ -d "$tmp_dir/lib/ollama" ]; then
    src_lib="$tmp_dir/lib/ollama"
  elif [ -d "$(dirname "$src_bin")/../lib/ollama" ]; then
    src_lib="$(cd "$(dirname "$src_bin")/../lib/ollama" && pwd)"
  fi
  if [ -n "$src_lib" ] && [ -d "$src_lib" ]; then
    local dest_lib="${extract_root}/lib/ollama"
    mkdir -p "$(dirname "$dest_lib")" 2>/dev/null || true
    # Replace lib tree atomically-ish: new temp sibling then rename.
    local new_lib
    new_lib="$(mktemp -d "${extract_root}/lib/ollama.new.XXXXXX" 2>/dev/null \
      || mktemp -d "${TMPDIR:-/tmp}/tr-ollama-lib.XXXXXX")"
    if cp -a "$src_lib/." "$new_lib/" 2>>"$RECALL_LOG_FILE"; then
      if [ -d "$dest_lib" ]; then
        local old_lib="${dest_lib}.old.$$"
        mv "$dest_lib" "$old_lib" 2>/dev/null || true
        if mv "$new_lib" "$dest_lib" 2>>"$RECALL_LOG_FILE"; then
          # Safe delete: only a path we just renamed this run under extract_root.
          if [ -n "$old_lib" ] && [ -d "$old_lib" ] && [ "$old_lib" != "/" ]; then
            find "$old_lib" -mindepth 1 -delete 2>/dev/null || true
            rmdir "$old_lib" 2>/dev/null || true
          fi
        else
          # Roll back rename if possible.
          [ -d "$old_lib" ] && mv "$old_lib" "$dest_lib" 2>/dev/null || true
          find "$new_lib" -mindepth 1 -delete 2>/dev/null || true
          rmdir "$new_lib" 2>/dev/null || true
        fi
      else
        mv "$new_lib" "$dest_lib" 2>>"$RECALL_LOG_FILE" || true
      fi
    else
      find "$new_lib" -mindepth 1 -delete 2>/dev/null || true
      rmdir "$new_lib" 2>/dev/null || true
    fi
  fi

  local got
  got="$(recall::_ollama_local_version "$dest" || true)"
  if [ -n "$got" ]; then
    printf '%s\n' "$got" >"${dest_bin}/.ollama-version" 2>/dev/null || true
  fi
  # Cache intended release as latest — prefer pin, else binary version.
  # Never write a stale server-reported version (see _ollama_local_version).
  local stamped="${pin:-$got}"
  if [ -n "$stamped" ]; then
    printf '%s\n' "$stamped" >"${dest_bin}/.ollama-latest" 2>/dev/null || true
  fi
  # Clear resolver cache so next recall::ollama re-picks.
  RECALL_OLLAMA_CACHED=""
  recall::log "install_ollama: ollama ready at $dest (${got:-unknown})"
  # If we replaced a live product daemon (or any product serve is down after
  # upgrade), bring product serve back on the product-owned URL so API version
  # always matches the embedded binary — never leave traffic on a foreign host.
  if ! recall::_daemon_is_product "$dest"; then
    recall::log "install_ollama: restarting product serve after binary install"
    recall::ollama_serve 2>>"${RECALL_LOG_FILE:-/dev/null}" || true
  fi
  return 0
}

# Ensure product-managed binary exists and is not older than latest release.
# No-op when RECALL_OLLAMA_AUTO_UPDATE=0 (still installs if missing).
# Soft: network failure leaves existing binary alone.
recall::_ensure_product_ollama_current() {
  local bundled="${RECALL_DATA_ROOT}/bin/ollama"
  local auto="${RECALL_OLLAMA_AUTO_UPDATE:-1}"

  if [ ! -x "$bundled" ]; then
    recall::_install_ollama || return 1
    return 0
  fi

  case "$auto" in
    0|false|no|off|FALSE|NO|OFF) return 0 ;;
  esac

  # Throttle version probes (default 24h) unless FORCE.
  local force="${RECALL_OLLAMA_FORCE_UPDATE:-0}"
  local check="${RECALL_DATA_ROOT}/bin/.ollama-check"
  local ttl="${RECALL_OLLAMA_UPDATE_TTL_S:-86400}"
  local now
  now="$(date +%s 2>/dev/null || echo 0)"
  if [ "$force" != "1" ] && [ -f "$check" ]; then
    local mtime age
    mtime="$(stat -c %Y "$check" 2>/dev/null || echo 0)"
    age=$((now - mtime))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$ttl" ]; then
      return 0
    fi
  fi
  printf '%s\n' "$now" >"$check" 2>/dev/null || true

  local local_v latest_v
  local_v="$(recall::_ollama_local_version "$bundled" || true)"
  latest_v="$(recall::_ollama_latest_version || true)"
  if [ -z "$latest_v" ]; then
    recall::log "ollama auto-update: could not resolve latest version; keeping ${local_v:-unknown}"
    return 0
  fi
  if [ -z "$local_v" ]; then
    recall::log "ollama auto-update: local version unreadable; reinstalling"
    recall::_install_ollama || return 1
    return 0
  fi
  if recall::_version_lt "$local_v" "$latest_v"; then
    recall::log "ollama auto-update: ${local_v} → ${latest_v} (product-managed binary)"
    recall::_install_ollama || {
      recall::log "ollama auto-update: install failed; keeping ${local_v}"
      return 1
    }
  else
    recall::log "ollama auto-update: product binary current (${local_v})"
  fi
  return 0
}

# Resolve the product-owned ollama binary. Always the embedded binary under
# ${RECALL_DATA_ROOT}/bin/ollama (auto-updated to GitHub latest). System PATH /
# snap ollama are never used for product work — opt in only with
# RECALL_OLLAMA_ALLOW_SYSTEM=1 (debug/rescue). Explicit RECALL_OLLAMA still wins.
#
# Product auto-update (default on): when the managed binary is missing or older
# than GitHub latest (or OLLAMA_VERSION pin), re-fetch. TTL 24h between probes
# (RECALL_OLLAMA_UPDATE_TTL_S). RECALL_OLLAMA_AUTO_UPDATE=0 disables bumps
# (still installs if missing). Critical for think:false + structured JSON:
# ollama ≥0.31.2 fixed that path; stale 0.30.x product bins silently regress.
#
# Prints the resolved path to stdout; returns non-zero if none found/fetchable.
recall::ollama() {
  if [ -n "$RECALL_OLLAMA_CACHED" ]; then
    printf '%s' "$RECALL_OLLAMA_CACHED"
    return 0
  fi

  local bundled="${RECALL_DATA_ROOT}/bin/ollama"

  # RECALL_OLLAMA explicit override (operator pin) — still product-facing.
  if [ -n "${RECALL_OLLAMA:-}" ] && recall::_can_run_ollama "$RECALL_OLLAMA"; then
    RECALL_OLLAMA_CACHED="$RECALL_OLLAMA"
    recall::log "ollama: using explicit RECALL_OLLAMA override $RECALL_OLLAMA"
    printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
  fi

  # Product-managed binary: install if missing, bump if behind latest.
  recall::_ensure_product_ollama_current 2>>"$RECALL_LOG_FILE" || true

  if recall::_can_run_ollama "$bundled"; then
    RECALL_OLLAMA_CACHED="$bundled"
    local ver
    ver="$(recall::_ollama_local_version "$bundled" 2>/dev/null || echo '?')"
    recall::log "ollama: product-embedded binary $bundled ($ver)"
    printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
  fi

  # Missing or broken — fetch once more hard.
  if recall::_install_ollama && recall::_can_run_ollama "$bundled"; then
    RECALL_OLLAMA_CACHED="$bundled"
    recall::log "ollama: product-embedded binary after install $bundled"
    printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
  fi

  # Opt-in system rescue only (not product path).
  case "${RECALL_OLLAMA_ALLOW_SYSTEM:-0}" in
    1|true|yes|on|TRUE|YES|ON)
      local sys=""
      command -v ollama >/dev/null 2>&1 && sys="$(command -v ollama)"
      if [ -z "$sys" ] && [ -x /snap/bin/ollama ]; then
        sys=/snap/bin/ollama
      fi
      if [ -n "$sys" ] && recall::_can_run_ollama "$sys"; then
        RECALL_OLLAMA_CACHED="$sys"
        recall::log "ollama: RECALL_OLLAMA_ALLOW_SYSTEM=1 using $sys (not product)"
        printf '%s' "$RECALL_OLLAMA_CACHED"; return 0
      fi
      ;;
  esac

  recall::log "ollama: product binary unavailable at $bundled (install failed)"
  return 1
}

# Product defaults for every ollama serve we start (GPU + MTP).
# Safe on runners that ignore unknown knobs. Qwen3.5 GGUFs ship built-in MTP
# heads; CUDA/llama-server auto-engages them. MLX uses the OLLAMA_MLX_MTP_* envs.
recall::_ollama_product_env() {
  export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
  export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"
  export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-4}"
  export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-4}"
  export OLLAMA_MAX_QUEUE="${OLLAMA_MAX_QUEUE:-2048}"
  # Multi-token prediction (MTP) draft depth — applied where the runner supports it.
  export OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS="${OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS:-4}"
  export OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS="${OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS:-4}"
  # Optional shared model store (e.g. host already has /var/lib/ollama models).
  # Prefer RECALL_OLLAMA_MODELS; fall through to OLLAMA_MODELS if already set.
  if [ -n "${RECALL_OLLAMA_MODELS:-}" ]; then
    export OLLAMA_MODELS="$RECALL_OLLAMA_MODELS"
  fi
}

# Start the product ollama daemon on the product-owned URL.
# Idempotent only when *our* binary is already serving that URL — a foreign
# daemon (system ollama on 11434, or anything else) never counts as "up".
# Logs to ${RECALL_LOG_DIR}/llm-provision.log.
recall::ollama_serve() {
  local ollama_bin base_url host_port log_file launcher daemon_pid i
  ollama_bin="$(recall::ollama)" || return 1
  base_url="$(recall::llm_base_url)"
  host_port="$(recall::llm_host_port)"

  if recall::_daemon_is_product "$ollama_bin"; then
    recall::_stamp_ollama_url "$base_url"
    # Export so same shell's pull/list hit the product daemon.
    export TOTAL_RECALL_LLM_BASE_URL="$base_url"
    export OLLAMA_HOST="$host_port"
    recall::log "ollama_serve: product daemon already serving ${base_url} (bin=$ollama_bin)"
    return 0
  fi

  if curl -sf --max-time 2 "${base_url}/api/tags" >/dev/null 2>&1; then
    # Port up but not our binary — never ride a foreign daemon.
    recall::log "ollama_serve: ${base_url} is up but not product binary ${ollama_bin} — not using foreign daemon"
    return 1
  fi

  log_file="${RECALL_LOG_DIR}/llm-provision.log"
  mkdir -p "$RECALL_LOG_DIR" 2>/dev/null || true
  launcher="nohup"
  if command -v setsid >/dev/null 2>&1; then launcher="setsid nohup"; fi
  recall::_ollama_product_env
  # Bind explicitly — never inherit a host OLLAMA_HOST pointing at system 11434.
  export OLLAMA_HOST="$host_port"
  export TOTAL_RECALL_LLM_BASE_URL="$base_url"
  recall::log "ollama_serve: starting product daemon at ${base_url} (OLLAMA_HOST=${host_port}, log: $log_file)"
  $launcher env \
    OLLAMA_HOST="${host_port}" \
    OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION}" \
    OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE}" \
    OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS}" \
    OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL}" \
    OLLAMA_MAX_QUEUE="${OLLAMA_MAX_QUEUE}" \
    OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS="${OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS}" \
    OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS="${OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS}" \
    ${OLLAMA_MODELS:+OLLAMA_MODELS="${OLLAMA_MODELS}"} \
    "$ollama_bin" serve >>"$log_file" 2>&1 < /dev/null &
  daemon_pid=$!
  disown 2>/dev/null || true
  printf '%s\n' "$daemon_pid" >"${RECALL_DATA_ROOT}/bin/ollama.pid" 2>/dev/null || true

  i=0
  while [ $i -lt 15 ]; do
    i=$(( i + 1 ))
    sleep 1
    if recall::_daemon_is_product "$ollama_bin"; then
      recall::_stamp_ollama_url "$base_url"
      recall::log "ollama_serve: product daemon up at ${base_url} (pid=$daemon_pid, waited ${i}s)"
      return 0
    fi
  done
  recall::log "ollama_serve: product daemon not confirmed at ${base_url} after 15s (pid=$daemon_pid)"
  return 1
}

# Pull a model on the *product* daemon (OLLAMA_HOST forced). Idempotent.
recall::ollama_pull() {
  local model="$1"
  local ollama_bin host_port log_file
  ollama_bin="$(recall::ollama)" || return 1
  host_port="$(recall::llm_host_port)"
  export OLLAMA_HOST="$host_port"
  if OLLAMA_HOST="$host_port" "$ollama_bin" list 2>/dev/null \
    | awk 'NR>1{print $1}' | grep -qx "${model}\(:latest\)\?"; then
    recall::log "ollama_pull: model ${model} already present (host=${host_port})"
    return 0
  fi
  recall::log "ollama_pull: pulling ${model} via ${host_port} — may be several minutes / GB-scale download"
  log_file="${RECALL_LOG_DIR}/llm-provision.log"
  OLLAMA_HOST="$host_port" "$ollama_bin" pull "${model}" >> "$log_file" 2>&1
}

# Product-owned ollama defaults (embed = hybrid dense; chat = optional refine).
recall::embed_model() {
  local m="${TOTAL_RECALL_EMBED_MODEL:-qwen3-embedding:0.6b}"
  # Reject legacy HF ids — fall back to recommended ollama tag.
  case "$m" in
    */*|*"gte-modernbert"*|*"bge-small-en"*)
      m="qwen3-embedding:0.6b"
      ;;
  esac
  printf '%s' "$m"
}

# True if dense embeds are wanted (default-on). TOTAL_RECALL_VEC=0 opts out.
recall::_want_embed() {
  case "${TOTAL_RECALL_VEC:-1}" in
    0|false|no|off|FALSE|NO|OFF) return 1 ;;
    *) return 0 ;;
  esac
}

# True if LLM chat refine is wanted. TOTAL_RECALL_LLM_PROVIDER=none opts out
# of *chat only* — embeds still use product ollama unless VEC=0 too.
recall::_want_llm_chat() {
  [ "${TOTAL_RECALL_LLM_PROVIDER:-auto}" = "none" ] && return 1
  return 0
}

# Orchestrator: product-owned ollama → serve → pull embed (+ chat if wanted).
# Idempotent. NEVER fatal: every failure path returns 0 (layers fail soft).
# Does NOT early-exit on .ollama_ready alone — must re-ensure models so a
# machine that only ever pulled chat still gets the embed model.
recall::provision_llm() {
  local log_file="${RECALL_LOG_DIR}/llm-provision.log"
  mkdir -p "$RECALL_LOG_DIR" 2>/dev/null || true

  local want_embed=0 want_chat=0
  recall::_want_embed && want_embed=1
  recall::_want_llm_chat && want_chat=1

  if [ "$want_embed" -eq 0 ] && [ "$want_chat" -eq 0 ]; then
    return 0
  fi

  local chat_model="${TOTAL_RECALL_LLM_MODEL:-qwen3.5:2b}"
  local embed_model
  embed_model="$(recall::embed_model)"
  local sentinel="${RECALL_DATA_ROOT}/.ollama_ready"

  {
    printf '[ollama-provision] %s start embed=%s chat=%s embed_model=%s chat_model=%s\n' \
      "$(date -Iseconds 2>/dev/null || date)" "$want_embed" "$want_chat" "$embed_model" "$chat_model"

    local ollama_bin
    if ! ollama_bin="$(recall::ollama)"; then
      printf '[ollama-provision] WARN: could not resolve or fetch ollama binary\n'
      return 0
    fi
    printf '[ollama-provision] binary: %s\n' "$ollama_bin"

    if ! recall::ollama_serve; then
      printf '[ollama-provision] WARN: daemon start failed\n'
      return 0
    fi

    local ok=1
    if [ "$want_embed" -eq 1 ]; then
      if ! recall::ollama_pull "$embed_model"; then
        printf '[ollama-provision] WARN: embed model pull failed: %s\n' "$embed_model"
        ok=0
      fi
    fi
    if [ "$want_chat" -eq 1 ]; then
      if ! recall::ollama_pull "$chat_model"; then
        printf '[ollama-provision] WARN: chat model pull failed: %s\n' "$chat_model"
        ok=0
      fi
    fi

    if [ "$ok" -eq 1 ]; then
      touch "$sentinel" 2>/dev/null || true
      printf '[ollama-provision] done — sentinel %s\n' "$sentinel"
    else
      printf '[ollama-provision] incomplete — sentinel not written\n'
    fi
  } >> "$log_file" 2>&1

  recall::log "provision_llm: complete (embed=$want_embed chat=$want_chat)"
  return 0
}

# Launch recall::provision_llm as a fully-detached sidecar. Returns immediately.
# Fires when embed and/or chat are wanted — not skipped solely by .ollama_ready
# (provision itself is cheap when models are already present).
recall::start_llm_provision() {
  local want_embed=0 want_chat=0
  recall::_want_embed && want_embed=1
  recall::_want_llm_chat && want_chat=1
  if [ "$want_embed" -eq 0 ] && [ "$want_chat" -eq 0 ]; then
    return 0
  fi

  mkdir -p "$RECALL_DATA_ROOT" "$RECALL_LOG_DIR" 2>/dev/null || return 0

  local launcher="nohup"
  if command -v setsid >/dev/null 2>&1; then launcher="setsid nohup"; fi

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
# Optional $1 is the hook event name (SessionStart / UserPromptSubmit); the
# banner text is hook-agnostic, so the arg is accepted for API compatibility.
recall::bootstrap_banner() {
  cat <<'EOF'
**[total-recall]** First-run indexing of ~/.claude/projects/ is happening in the background.

Past-session memory will be available once it finishes (typically 15–90 seconds for a few GB of transcripts). Run `/total-recall:recall-health` to check progress.

This message will not repeat — recall results will start surfacing automatically.
EOF
}
