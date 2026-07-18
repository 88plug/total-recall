#!/usr/bin/env bash
# Hermetic tests for the LLM-provision functions in hooks/lib/common.sh.
# No network access, no real ollama binary, no real model downloads.
# Mock curl and ollama via shims on a temp PATH.
#
# Run: bash tests/test_llm_provision.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMMON_SH="$REPO_ROOT/hooks/lib/common.sh"

FAIL=0
PASS=0

ok()   { printf "  ok  %s\n" "$1"; PASS=$(( PASS + 1 )); }
fail() { printf "  FAIL %s — %s\n" "$1" "$2"; FAIL=$(( FAIL + 1 )); }

# Isolate all plugin-data writes.
TMPDATA="$(mktemp -d)"
STUBS="$(mktemp -d)"
trap 'rm -rf "$TMPDATA" "$STUBS"' EXIT

export CLAUDE_PLUGIN_DATA="$TMPDATA"

# ---------------------------------------------------------------------------
# Helper: source common.sh in a controlled subshell.
# $RECALL_OLLAMA and RECALL_OLLAMA_CACHED are cleared so each test starts
# fresh unless the test explicitly sets them.
#
# We also exclude /snap/bin from the PATH so the snap install never shadows
# our stubs — the tests that care about snap resolution use $RECALL_OLLAMA.
# ---------------------------------------------------------------------------
SAFE_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '^/snap' | tr '\n' ':' | sed 's/:$//')"

run_in_subshell() {
  # $1 = bash code to eval
  local code="$1"
  bash -c "
    export PATH='${STUBS}:${SAFE_PATH}'
    export CLAUDE_PLUGIN_DATA='${TMPDATA}'
    export RECALL_OLLAMA=''
    export RECALL_OLLAMA_CACHED=''
    export RECALL_UV_CACHED=''
    source '${COMMON_SH}'
    ${code}
  " 2>/dev/null
}

# Variant that captures stdout + stores exit code in a temp file.
run_capture() {
  local code="$1"
  local rc_file; rc_file="$(mktemp)"
  local out
  out="$(bash -c "
    export PATH='${STUBS}:${SAFE_PATH}'
    export CLAUDE_PLUGIN_DATA='${TMPDATA}'
    export RECALL_OLLAMA=''
    export RECALL_OLLAMA_CACHED=''
    export RECALL_UV_CACHED=''
    source '${COMMON_SH}'
    ${code}
  " 2>/dev/null; echo $? > '${rc_file}')"
  # rc_file written by the subshell; read it back
  local rc=0
  rc="$(cat "${rc_file}" 2>/dev/null || echo 0)"
  rm -f "${rc_file}"
  printf '%s' "$out"
  return "$rc"
}

# ---------------------------------------------------------------------------
# Stub factories
# ---------------------------------------------------------------------------
write_stub() {
  local name="$1" body="$2"
  printf '#!/usr/bin/env bash\n%s\n' "$body" > "$STUBS/$name"
  chmod +x "$STUBS/$name"
}
rm_stub() { rm -f "$STUBS/$1"; }

# ---------------------------------------------------------------------------
# 0. Syntax check
# ---------------------------------------------------------------------------
echo "[0] syntax check"
if bash -n "$COMMON_SH" 2>/dev/null; then
  ok "common.sh: bash -n passes"
else
  fail "common.sh" "bash -n syntax error"
fi

if bash -n "$REPO_ROOT/scripts/llm-setup.sh" 2>/dev/null; then
  ok "llm-setup.sh: bash -n passes"
else
  fail "llm-setup.sh" "bash -n syntax error"
fi

# ---------------------------------------------------------------------------
# 1. Full opt-out: LLM_PROVIDER=none AND VEC=0 → no ollama at all.
# ---------------------------------------------------------------------------
echo "[1] full opt-out via TOTAL_RECALL_LLM_PROVIDER=none + TOTAL_RECALL_VEC=0"

CURL_CALL_LOG="$TMPDATA/curl_calls.txt"
rm -f "$CURL_CALL_LOG"

write_stub "curl"   "echo CURL_CALLED >> '$CURL_CALL_LOG'; exit 0"
write_stub "ollama" "echo OLLAMA_CALLED >> '$CURL_CALL_LOG'; exit 0"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_PROVIDER=none
  export TOTAL_RECALL_VEC=0
  recall::provision_llm
"
RC=$?
set -e

if [ "$RC" = "0" ]; then
  ok "full opt-out: provision_llm returns 0"
else
  fail "full opt-out" "expected rc=0 got $RC"
fi

if [ ! -f "$CURL_CALL_LOG" ]; then
  ok "full opt-out: curl/ollama not called"
else
  fail "full opt-out" "curl or ollama was called: $(cat "$CURL_CALL_LOG")"
fi

rm_stub "curl"
rm_stub "ollama"

# ---------------------------------------------------------------------------
# 2. start_llm_provision is a no-op when both layers disabled
# ---------------------------------------------------------------------------
echo "[2] start_llm_provision no-op when LLM=none and VEC=0"

SIDECAR_LOG="$TMPDATA/sidecar.txt"
rm -f "$SIDECAR_LOG"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_PROVIDER=none
  export TOTAL_RECALL_VEC=0
  recall::start_llm_provision
  wait
" > "$SIDECAR_LOG" 2>&1
set -e

if [ ! -s "$SIDECAR_LOG" ] || ! grep -qi "ollama" "$SIDECAR_LOG" 2>/dev/null; then
  ok "start_llm_provision: no-op when both layers off"
else
  fail "start_llm_provision" "unexpected output: $(cat "$SIDECAR_LOG")"
fi

# ---------------------------------------------------------------------------
# 3. recall::ollama resolution order
# ---------------------------------------------------------------------------
echo "[3] recall::ollama resolution order"

# 3a — RECALL_OLLAMA env override (explicit path, shadows snap/PATH).
FAKE_OLLAMA="$TMPDATA/my-ollama"
printf '#!/usr/bin/env bash\necho "ollama version 0.0.0-test"\n' > "$FAKE_OLLAMA"
chmod +x "$FAKE_OLLAMA"

set +e
RESULT=$(run_in_subshell "
  export RECALL_OLLAMA='$FAKE_OLLAMA'
  recall::ollama
")
RC=$?
set -e
if [ "$RC" = "0" ] && [ "$RESULT" = "$FAKE_OLLAMA" ]; then
  ok "ollama resolver: RECALL_OLLAMA env override"
else
  fail "ollama resolver (RECALL_OLLAMA)" "rc=$RC result='$RESULT' (expected '$FAKE_OLLAMA')"
fi

# 3b — ollama on PATH when product binary missing and install blocked.
#       Product auto-fetch would otherwise win (and hit the network).
write_stub "curl" "exit 1"
write_stub "ollama" 'echo "ollama version 0.0.0-stub"; exit 0'
rm -f "$TMPDATA/total-recall/bin/ollama" 2>/dev/null || true

set +e
RESULT=$(run_in_subshell "
  export RECALL_OLLAMA=''
  export RECALL_OLLAMA_AUTO_UPDATE=0
  recall::ollama
")
RC=$?
set -e
if [ "$RC" = "0" ] && [ "$RESULT" = "$STUBS/ollama" ]; then
  ok "ollama resolver: stub on PATH when product bin missing"
else
  fail "ollama resolver (PATH)" "rc=$RC result='$RESULT' (expected '$STUBS/ollama')"
fi
rm_stub "ollama"
rm_stub "curl"

# 3c — product-managed data-root binary preferred over PATH (no pin).
DATA_OLLAMA_DIR="$TMPDATA/total-recall/bin"
mkdir -p "$DATA_OLLAMA_DIR"
BUNDLED_OLLAMA="$DATA_OLLAMA_DIR/ollama"
printf '#!/usr/bin/env bash\necho "ollama version is 0.32.1"\n' > "$BUNDLED_OLLAMA"
chmod +x "$BUNDLED_OLLAMA"
write_stub "ollama" 'echo "ollama version is 0.30.10"; exit 0'
# Freeze auto-update so we don't replace the fixture with a network download.
write_stub "curl" "exit 1"

set +e
RESULT=$(run_in_subshell "
  export RECALL_OLLAMA=''
  export RECALL_OLLAMA_AUTO_UPDATE=0
  export RECALL_OLLAMA_FORCE_UPDATE=0
  recall::ollama
")
RC=$?
set -e
if [ "$RC" = "0" ] && [ "$RESULT" = "$BUNDLED_OLLAMA" ]; then
  ok "ollama resolver: product-managed binary preferred over PATH"
else
  fail "ollama resolver (product prefer)" "rc=$RC result='$RESULT' (expected '$BUNDLED_OLLAMA')"
fi
rm_stub "ollama"
rm_stub "curl"
rm -f "$BUNDLED_OLLAMA"

# ---------------------------------------------------------------------------
# 4. Arch detection maps correctly.
# ---------------------------------------------------------------------------
echo "[4] arch detection"

check_arch() {
  local uname_output="$1"
  local expected="$2"
  set +e
  RESULT=$(bash -c "
    uname() { if [ \"\$1\" = '-m' ]; then printf '%s' '${uname_output}'; else command uname \"\$@\"; fi; }
    export -f uname
    source '${COMMON_SH}' 2>/dev/null
    recall::_ollama_arch
  " 2>/dev/null)
  RC=$?
  set -e
  if [ "$RC" = "0" ] && [ "$RESULT" = "$expected" ]; then
    ok "arch: $uname_output → $expected"
  else
    fail "arch ($uname_output)" "expected '$expected' rc=0, got rc=$RC result='$RESULT'"
  fi
}

check_arch "x86_64"  "amd64"
check_arch "aarch64" "arm64"
check_arch "arm64"   "arm64"

# Unsupported arch returns non-zero.
set +e
bash -c "
  uname() { if [ \"\$1\" = '-m' ]; then printf '%s' 'mips'; else command uname \"\$@\"; fi; }
  export -f uname
  source '${COMMON_SH}' 2>/dev/null
  recall::_ollama_arch
" 2>/dev/null
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  ok "arch: unsupported returns non-zero"
else
  fail "arch (unsupported)" "expected non-zero, got 0"
fi

# ---------------------------------------------------------------------------
# 5. Idempotent re-provision: daemon up + both models listed → no pull.
#    (sentinel alone no longer skips — must still ensure models.)
# ---------------------------------------------------------------------------
echo "[5] idempotent re-provision when models already present"

PULL_LOG="$TMPDATA/pull_idempotent.txt"
rm -f "$PULL_LOG"

write_stub "curl" 'exit 0'  # daemon reachable
write_stub "ollama" "
case \"\$1\" in
  --version) echo 'ollama version 0.0.0-stub'; exit 0;;
  list)  printf 'NAME\nid\nsize\nqwen3-embedding:0.6b\tx\t1B\nqwen3.5:2b\ty\t1B\n'; exit 0;;
  pull)  echo pull_called >> '$PULL_LOG'; exit 0;;
  serve) exit 0;;
  *)     exit 0;;
esac
"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_PROVIDER=auto
  export TOTAL_RECALL_VEC=1
  export RECALL_OLLAMA='$STUBS/ollama'
  recall::provision_llm
"
RC=$?
set -e

if [ "$RC" = "0" ]; then
  ok "idempotent: provision_llm returns 0 when models present"
else
  fail "idempotent" "expected rc=0 got $RC"
fi

if [ ! -f "$PULL_LOG" ]; then
  ok "idempotent: ollama pull not invoked when models listed"
else
  fail "idempotent" "unexpected pull: $(cat "$PULL_LOG")"
fi

rm_stub "curl"
rm_stub "ollama"
rm -f "$PULL_LOG"

# ---------------------------------------------------------------------------
# 5b. LLM_PROVIDER=none still pulls embed model (product dense path).
# ---------------------------------------------------------------------------
echo "[5b] LLM=none still provisions embed model"

PULL_LOG="$TMPDATA/pull_embed_only.txt"
rm -f "$PULL_LOG"
write_stub "curl" 'exit 0'
write_stub "ollama" "
case \"\$1\" in
  --version) echo 'ollama version 0.0.0-stub'; exit 0;;
  list)  printf 'NAME\nid\n'; exit 0;;
  pull)
    echo \"\$2\" >> '$PULL_LOG'
    exit 0;;
  serve) exit 0;;
  *) exit 0;;
esac
"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_PROVIDER=none
  export TOTAL_RECALL_VEC=1
  export RECALL_OLLAMA='$STUBS/ollama'
  recall::provision_llm
"
RC=$?
set -e

if [ "$RC" = "0" ] && grep -q 'qwen3-embedding:0.6b' "$PULL_LOG" 2>/dev/null; then
  ok "LLM=none: embed model still pulled"
else
  fail "LLM=none embed" "rc=$RC pulls='$(cat "$PULL_LOG" 2>/dev/null)'"
fi

if ! grep -q 'qwen3.5:2b' "$PULL_LOG" 2>/dev/null; then
  ok "LLM=none: chat model not pulled"
else
  fail "LLM=none chat" "chat model was pulled: $(cat "$PULL_LOG")"
fi

rm_stub "curl"
rm_stub "ollama"
rm -f "$PULL_LOG"

# ---------------------------------------------------------------------------
# 6. recall::ollama_serve — no-op when daemon already reachable.
# ---------------------------------------------------------------------------
echo "[6] ollama_serve no-op when daemon reachable"

write_stub "curl"   'exit 0'   # pretend /api/tags returns 200
SERVE_LOG="$TMPDATA/serve_called.txt"
rm -f "$SERVE_LOG"
write_stub "ollama" "echo serve_called >> '$SERVE_LOG'; exit 0"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_BASE_URL=http://localhost:11434
  recall::ollama_serve
"
RC=$?
set -e

if [ "$RC" = "0" ]; then
  ok "ollama_serve: returns 0 when daemon reachable"
else
  fail "ollama_serve (reachable)" "rc=$RC"
fi

if [ ! -f "$SERVE_LOG" ]; then
  ok "ollama_serve: 'ollama serve' not invoked when daemon already up"
else
  fail "ollama_serve (reachable)" "unexpectedly invoked serve: $(cat "$SERVE_LOG")"
fi

rm_stub "curl"
rm_stub "ollama"
rm -f "$SERVE_LOG"

# ---------------------------------------------------------------------------
# 7. recall::ollama_pull — skips pull when model already listed.
# ---------------------------------------------------------------------------
echo "[7] ollama_pull skips when model present"

PULL_LOG="$TMPDATA/pull_called.txt"
rm -f "$PULL_LOG"

# Stub: list returns the model we want; pull would mark the log.
write_stub "ollama" "
case \"\$1\" in
  --version) echo 'ollama version 0.0.0-stub'; exit 0;;
  list)  printf 'NAME\t\t\tID\t\t\tSIZE\tMODIFIED\nqwen3.5:2b\taaaa\t2.7G\t1 day ago\n'; exit 0;;
  pull)  echo pull_called >> '$PULL_LOG'; exit 0;;
  *)     exit 0;;
esac
"

set +e
run_in_subshell "
  recall::ollama_pull 'qwen3.5:2b'
"
RC=$?
set -e

if [ "$RC" = "0" ]; then
  ok "ollama_pull: returns 0 when model present"
else
  fail "ollama_pull (present)" "rc=$RC"
fi

if [ ! -f "$PULL_LOG" ]; then
  ok "ollama_pull: pull not called when model already listed"
else
  fail "ollama_pull (present)" "pull was called: $(cat "$PULL_LOG")"
fi

rm_stub "ollama"
rm -f "$PULL_LOG"

# ---------------------------------------------------------------------------
# 8. recall::_install_ollama — fetch failure returns non-zero.
#    We test the internal installer directly (bypassing the full resolver which
#    would find /snap/bin/ollama or a system install on a provisioned machine).
# ---------------------------------------------------------------------------
echo "[8] _install_ollama: curl failure returns non-zero"

write_stub "curl" "exit 1"
rm -f "$TMPDATA/total-recall/bin/ollama" 2>/dev/null || true

set +e
run_in_subshell "
  recall::_install_ollama
"
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
  ok "_install_ollama: curl failure returns non-zero"
else
  fail "_install_ollama (fetch fail)" "expected non-zero, got 0"
fi

rm_stub "curl"

# ---------------------------------------------------------------------------
# 8b. version helpers + auto-update skip when current.
# ---------------------------------------------------------------------------
echo "[8b] ollama version helpers / auto-update"

set +e
run_in_subshell '
  recall::_version_lt 0.30.10 0.32.1
'
RC=$?
set -e
if [ "$RC" -eq 0 ]; then
  ok "_version_lt: 0.30.10 < 0.32.1"
else
  fail "_version_lt (older)" "expected 0, got $RC"
fi

set +e
run_in_subshell '
  recall::_version_lt 0.32.1 0.32.1
'
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  ok "_version_lt: equal is not less"
else
  fail "_version_lt (equal)" "expected non-zero"
fi

# local version parse
write_stub "ollama" 'echo "ollama version is 0.32.1"; exit 0'
set +e
VER_OUT="$(run_in_subshell '
  recall::_ollama_local_version "$(command -v ollama)"
')"
RC=$?
set -e
if [ "$RC" -eq 0 ] && [ "$VER_OUT" = "0.32.1" ]; then
  ok "_ollama_local_version parses 0.32.1"
else
  fail "_ollama_local_version" "got rc=$RC out=$VER_OUT"
fi
rm_stub "ollama"

# auto-update: missing binary + curl fail → non-zero, soft at resolver layer
write_stub "curl" "exit 1"
rm -f "$TMPDATA/total-recall/bin/ollama" 2>/dev/null || true
set +e
run_in_subshell '
  export RECALL_OLLAMA_FORCE_UPDATE=1
  recall::_ensure_product_ollama_current
'
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  ok "_ensure_product_ollama_current: missing+curl fail returns non-zero"
else
  fail "_ensure_product_ollama_current (fail)" "expected non-zero"
fi
rm_stub "curl"

# ---------------------------------------------------------------------------
# 9. recall::provision_llm — full happy path with stubs (no sentinel).
# ---------------------------------------------------------------------------
echo "[9] provision_llm happy path (stubbed)"

SENTINEL="$TMPDATA/total-recall/.ollama_ready"
rm -f "$SENTINEL"

# curl stub: succeed for /api/tags (daemon "up").
write_stub "curl" 'exit 0'
# ollama stub: --version, list, pull all succeed.
write_stub "ollama" "
case \"\$1\" in
  --version) echo 'ollama version 0.0.0-stub'; exit 0;;
  list)  printf 'NAME\t\t\tID\t\t\tSIZE\tMODIFIED\nqwen3.5:2b\tbbbb\t2.7G\t0 secs ago\n'; exit 0;;
  pull)  echo 'pulling stub noop'; exit 0;;
  serve) sleep 30; exit 0;;
  *)     exit 0;;
esac
"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_PROVIDER=auto
  export TOTAL_RECALL_LLM_MODEL=qwen3.5:2b
  export TOTAL_RECALL_LLM_BASE_URL=http://localhost:11434
  recall::provision_llm
"
RC=$?
set -e

if [ "$RC" = "0" ]; then
  ok "provision_llm: happy path returns 0"
else
  fail "provision_llm (happy path)" "rc=$RC"
fi

if [ -f "$SENTINEL" ]; then
  ok "provision_llm: sentinel .ollama_ready written"
else
  fail "provision_llm (sentinel)" ".ollama_ready not created (data_root=$TMPDATA/total-recall)"
fi

rm_stub "curl"
rm_stub "ollama"

# ---------------------------------------------------------------------------
# 10. recall::provision_llm — failure inside is swallowed (returns 0).
# ---------------------------------------------------------------------------
echo "[10] provision_llm failure is swallowed (non-fatal)"

rm -f "$TMPDATA/total-recall/.ollama_ready"

# curl: fail → daemon appears unreachable. ollama: fail --version → not usable.
write_stub "curl"   "exit 1"
write_stub "ollama" "exit 1"

set +e
run_in_subshell "
  export TOTAL_RECALL_LLM_PROVIDER=auto
  recall::provision_llm
"
RC=$?
set -e

if [ "$RC" = "0" ]; then
  ok "provision_llm: failure is non-fatal (returns 0)"
else
  fail "provision_llm (failure swallowed)" "expected rc=0 got $RC"
fi

rm_stub "curl"
rm_stub "ollama"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
printf "passed: %s    failed: %s\n" "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
