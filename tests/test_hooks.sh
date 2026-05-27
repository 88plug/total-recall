#!/usr/bin/env bash
# Tests for total-recall hook scripts. No DB required — we test the
# defensive paths and verify each hook (1) exits 0, (2) either produces an
# empty stdout or a valid JSON envelope.
#
# Run: bash tests/test_hooks.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK_DIR="$REPO_ROOT/hooks"

FAIL=0
PASS=0

# Isolate plugin-data writes into a tmp dir so the test never pollutes real state.
TMPDATA="$(mktemp -d)"
trap 'rm -rf "$TMPDATA"' EXIT
export CLAUDE_PLUGIN_DATA="$TMPDATA"

ok()   { printf "  ok  %s\n" "$1"; PASS=$((PASS+1)); }
fail() { printf "  FAIL %s — %s\n" "$1" "$2"; FAIL=$((FAIL+1)); }

# stdout is either empty/whitespace, OR a single line of valid JSON containing
# .hookSpecificOutput.{hookEventName,additionalContext}.
assert_envelope_or_empty() {
  local name="$1" out="$2" expected_event="${3:-}"
  if [ -z "${out// }" ]; then
    ok "$name: empty stdout (no memory available)"
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    ok "$name: stdout present, jq missing — skipping JSON validation"
    return 0
  fi
  if ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
    fail "$name" "non-empty stdout is not valid JSON: $out"
    return 1
  fi
  local got_evt got_ctx
  got_evt="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.hookEventName // empty')"
  got_ctx="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // empty')"
  if [ -z "$got_evt" ] || [ -z "$got_ctx" ]; then
    fail "$name" "envelope missing hookSpecificOutput.hookEventName or .additionalContext"
    return 1
  fi
  if [ -n "$expected_event" ] && [ "$got_evt" != "$expected_event" ]; then
    fail "$name" "envelope hookEventName: expected '$expected_event' got '$got_evt'"
    return 1
  fi
  ok "$name: valid envelope (event=$got_evt, ${#got_ctx} chars)"
}

# ---------------------------------------------------------------------------
# 0. Static checks: every .sh chmod +x, hooks.json is valid JSON.
# ---------------------------------------------------------------------------
echo "[0] static checks"

for f in "$HOOK_DIR"/*.sh "$HOOK_DIR"/lib/common.sh; do
  if [ ! -f "$f" ]; then
    fail "static" "missing file: $f"; continue
  fi
  if [ ! -x "$f" ]; then
    fail "static" "not executable: $f"
  else
    ok "static: executable $(basename "$f")"
  fi
done

if command -v jq >/dev/null 2>&1; then
  if jq -e . "$HOOK_DIR/hooks.json" >/dev/null 2>&1; then
    ok "static: hooks.json is valid JSON"
  else
    fail "static" "hooks.json is not valid JSON"
  fi
else
  ok "static: jq missing — skipping hooks.json validation"
fi

# Verify hooks.json shape (matcher, async flags, command paths).
if command -v jq >/dev/null 2>&1; then
  matcher="$(jq -r '.hooks.SessionStart[0].matcher // empty' "$HOOK_DIR/hooks.json")"
  if [ "$matcher" = "startup|clear" ]; then
    ok "static: SessionStart matcher is 'startup|clear' (defers compact/resume to amnesia)"
  else
    fail "static" "SessionStart matcher should be 'startup|clear', got '$matcher'"
  fi

  for evt in UserPromptSubmit Stop PostCompact; do
    a="$(jq -r ".hooks.$evt[0].hooks[0].async // false" "$HOOK_DIR/hooks.json")"
    if [ "$a" = "true" ]; then
      ok "static: $evt is async"
    else
      fail "static" "$evt should be async=true, got '$a'"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 1. With no DB available: every hook should exit 0 and emit empty stdout.
# ---------------------------------------------------------------------------
echo "[1] no-DB behavior"

# Make sure the default DB path inside our tmp dir doesn't exist.
rm -f "$TMPDATA/total-recall/index.db" 2>/dev/null

for hook in session-start-signpost user-prompt-retrieve stop-index post-compact-index; do
  case "$hook" in
    session-start-signpost)
      INPUT='{"session_id":"test-sess","cwd":"/tmp/none","hook_event_name":"SessionStart","source":"startup"}'
      EVT="SessionStart"
      ;;
    user-prompt-retrieve)
      INPUT='{"session_id":"test-sess","cwd":"/tmp/none","hook_event_name":"UserPromptSubmit","prompt":"a long enough prompt about widgets"}'
      EVT="UserPromptSubmit"
      ;;
    stop-index)
      INPUT='{"session_id":"test-sess","cwd":"/tmp/none","hook_event_name":"Stop"}'
      EVT=""
      ;;
    post-compact-index)
      INPUT='{"session_id":"test-sess","cwd":"/tmp/none","hook_event_name":"PostCompact"}'
      EVT=""
      ;;
  esac

  set +e
  OUT="$(printf '%s' "$INPUT" | "$HOOK_DIR/$hook.sh" 2>/dev/null)"
  RC=$?
  set -e

  if [ "$RC" -ne 0 ]; then
    fail "$hook (no DB)" "exit $RC, expected 0"
  else
    ok "$hook (no DB): exit 0"
  fi

  if [ "$hook" = "session-start-signpost" ] || [ "$hook" = "user-prompt-retrieve" ]; then
    assert_envelope_or_empty "$hook (no DB)" "$OUT" "$EVT"
  else
    # Indexer hooks should never write to stdout.
    if [ -n "${OUT// }" ]; then
      fail "$hook (no DB)" "expected empty stdout from indexer hook, got: $OUT"
    else
      ok "$hook (no DB): empty stdout (indexer is silent)"
    fi
  fi
done

# ---------------------------------------------------------------------------
# 2. Short/garbage prompts should make user-prompt-retrieve exit silently
#    even if a DB were present.
# ---------------------------------------------------------------------------
echo "[2] user-prompt-retrieve skip conditions"

for prompt in "" "hi" "<tool-stub />"; do
  INPUT="$(jq -n --arg p "$prompt" \
    '{session_id:"s",cwd:"/tmp/none",hook_event_name:"UserPromptSubmit",prompt:$p}' 2>/dev/null \
    || printf '{"session_id":"s","cwd":"/tmp/none","hook_event_name":"UserPromptSubmit","prompt":"%s"}' "$prompt")"
  set +e
  OUT="$(printf '%s' "$INPUT" | "$HOOK_DIR/user-prompt-retrieve.sh" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -z "${OUT// }" ]; then
    ok "user-prompt-retrieve skips prompt='$prompt'"
  else
    fail "user-prompt-retrieve skip" "rc=$RC out='$OUT' for prompt='$prompt'"
  fi
done

# ---------------------------------------------------------------------------
# 3. query.py: signpost and prompt-relevant should both succeed with empty
#    output when there's no DB.
# ---------------------------------------------------------------------------
echo "[3] query.py defensive paths"

if command -v python3 >/dev/null 2>&1; then
  set +e
  OUT="$(python3 "$HOOK_DIR/lib/query.py" signpost --cwd /tmp/none --max-tokens 200 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -z "${OUT// }" ]; then
    ok "query.py signpost (no DB): exit 0, empty stdout"
  else
    fail "query.py signpost" "rc=$RC out='$OUT'"
  fi

  set +e
  OUT="$(python3 "$HOOK_DIR/lib/query.py" prompt-relevant \
            --prompt "find the widget that broke yesterday" \
            --cwd /tmp/none --limit 3 --max-tokens 400 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -z "${OUT// }" ]; then
    ok "query.py prompt-relevant (no DB): exit 0, empty stdout"
  else
    fail "query.py prompt-relevant" "rc=$RC out='$OUT'"
  fi
else
  ok "query.py: python3 missing — skipping"
fi

# ---------------------------------------------------------------------------
# 4. Populated-DB simulation: stub `index.query` + a writable DB so signpost
#    actually emits markdown + an envelope.
# ---------------------------------------------------------------------------
echo "[4] populated-DB signpost"

if command -v python3 >/dev/null 2>&1; then
  STUB_DIR="$(mktemp -d)"
  mkdir -p "$STUB_DIR/index"
  # Marker file so query.py can detect a DB exists.
  mkdir -p "$TMPDATA/total-recall"
  touch "$TMPDATA/total-recall/index.db"

  cat > "$STUB_DIR/index/__init__.py" <<'PY'
PY
  cat > "$STUB_DIR/index/db.py" <<'PY'
import os
from pathlib import Path
DEFAULT_DB_PATH = Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "total-recall" / "index.db"
def connect(read_only=False):
    class FakeConn:
        pass
    return FakeConn()
PY
  cat > "$STUB_DIR/index/query.py" <<'PY'
def signpost_for_cwd(conn, cwd):
    return {
        "session_count": 3,
        "topics": ["sqlite indexer", "MCP server", "hook design"],
        "rules": ["prefer streaming over slurping"],
    }
def search_relevant(conn, prompt, cwd, limit):
    return [
        {"title": "earlier debugging of widget", "snippet": "found the off-by-one", "score": 0.91},
    ]
PY

  set +e
  OUT="$(printf '%s' '{"session_id":"s","cwd":"/tmp/x","hook_event_name":"SessionStart","source":"startup"}' \
    | env PYTHONPATH="$STUB_DIR" "$HOOK_DIR/session-start-signpost.sh" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -n "${OUT// }" ]; then
    assert_envelope_or_empty "signpost (populated)" "$OUT" "SessionStart"
  else
    fail "signpost (populated)" "rc=$RC out='$OUT'"
  fi

  set +e
  OUT="$(printf '%s' '{"session_id":"s","cwd":"/tmp/x","hook_event_name":"UserPromptSubmit","prompt":"find the broken widget"}' \
    | env PYTHONPATH="$STUB_DIR" "$HOOK_DIR/user-prompt-retrieve.sh" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -n "${OUT// }" ]; then
    assert_envelope_or_empty "prompt-relevant (populated)" "$OUT" "UserPromptSubmit"
  else
    fail "prompt-relevant (populated)" "rc=$RC out='$OUT'"
  fi

  rm -rf "$STUB_DIR"
  rm -f "$TMPDATA/total-recall/index.db"
else
  ok "populated-DB: python3 missing — skipping"
fi

# ---------------------------------------------------------------------------
# 5. _prompt_to_fts_query: natural-language prompts get tokenized + OR-joined.
# ---------------------------------------------------------------------------
echo "[5] _prompt_to_fts_query tokenization"

if command -v python3 >/dev/null 2>&1; then
  # Multi-word natural-language prompt should drop stopwords/short tokens and
  # produce an OR-joined quoted FTS5 query.
  set +e
  OUT="$(python3 -c "
import sys
sys.path.insert(0, '$HOOK_DIR/lib')
import query
print(query._prompt_to_fts_query('what did we decide about provider-x'))
" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ "$OUT" = '"decide" OR "provider-x"' ]; then
    ok "_prompt_to_fts_query: stopwords dropped, OR-joined ('$OUT')"
  else
    fail "_prompt_to_fts_query (multi-word)" "rc=$RC out='$OUT'"
  fi

  # All-stopword input collapses to empty (signal: skip the query).
  set +e
  OUT="$(python3 -c "
import sys
sys.path.insert(0, '$HOOK_DIR/lib')
import query
print(repr(query._prompt_to_fts_query('what is it')))
" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ "$OUT" = "''" ]; then
    ok "_prompt_to_fts_query: all-stopwords returns empty"
  else
    fail "_prompt_to_fts_query (stopwords-only)" "rc=$RC out='$OUT'"
  fi

  # Empty string in -> empty string out.
  set +e
  OUT="$(python3 -c "
import sys
sys.path.insert(0, '$HOOK_DIR/lib')
import query
print(repr(query._prompt_to_fts_query('')))
" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ "$OUT" = "''" ]; then
    ok "_prompt_to_fts_query: empty -> empty"
  else
    fail "_prompt_to_fts_query (empty)" "rc=$RC out='$OUT'"
  fi

  # Edge punctuation stripped; embedded chars preserved.
  set +e
  OUT="$(python3 -c "
import sys
sys.path.insert(0, '$HOOK_DIR/lib')
import query
print(query._prompt_to_fts_query('Decide?  provider-x-vps'))
" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ "$OUT" = '"decide" OR "provider-x-vps"' ]; then
    ok "_prompt_to_fts_query: edge punctuation stripped, internal chars kept"
  else
    fail "_prompt_to_fts_query (punctuation)" "rc=$RC out='$OUT'"
  fi
else
  ok "_prompt_to_fts_query: python3 missing — skipping"
fi

# ---------------------------------------------------------------------------
# 6. cmd_prompt_relevant: natural-language sentence reaches search_extractions
#    as an OR-joined FTS5 query (the real V7 docker bug — confirm fix).
# ---------------------------------------------------------------------------
echo "[6] cmd_prompt_relevant feeds OR-joined FTS query to search_extractions"

if command -v python3 >/dev/null 2>&1; then
  STUB_DIR="$(mktemp -d)"
  mkdir -p "$STUB_DIR/index"
  CAPTURE="$STUB_DIR/captured.txt"
  mkdir -p "$TMPDATA/total-recall"
  touch "$TMPDATA/total-recall/index.db"

  cat > "$STUB_DIR/index/__init__.py" <<'PY'
PY
  cat > "$STUB_DIR/index/db.py" <<'PY'
import os
from pathlib import Path
DEFAULT_DB_PATH = Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "total-recall" / "index.db"
def connect(read_only=False):
    class FakeConn:
        pass
    return FakeConn()
PY
  cat > "$STUB_DIR/index/query.py" <<PY
import os
CAPTURE = "$CAPTURE"
def _fts_match_quote(q):
    # Mirror real behavior: each whitespace term quoted (and-joined). We only
    # call this on single tokens from the hook's tokenizer, so output is a
    # single quoted phrase.
    if not q:
        return ""
    parts = []
    for t in q.split():
        parts.append('"' + t.replace('"', '""') + '"')
    return " ".join(parts)
def search_extractions(conn, query, cwd, kind, scope, since, limit):
    with open(CAPTURE, "w") as f:
        f.write(query or "")
    return [
        {"title": "provider-x decision", "snippet": "we chose provider-x for the eu pop", "score": 0.88},
    ]
def search_messages(conn, query, cwd, role, limit):
    return []
PY

  set +e
  OUT="$(printf '%s' '{"session_id":"s","cwd":"/home/operator/ip-service-for-docker","hook_event_name":"UserPromptSubmit","prompt":"what did we decide about provider-x"}' \
    | env PYTHONPATH="$STUB_DIR" "$HOOK_DIR/user-prompt-retrieve.sh" 2>/dev/null)"
  RC=$?
  set -e

  if [ "$RC" -ne 0 ]; then
    fail "cmd_prompt_relevant (sentence)" "rc=$RC"
  else
    if [ -f "$CAPTURE" ]; then
      CAPTURED="$(cat "$CAPTURE")"
      if [ "$CAPTURED" = '"decide" OR "provider-x"' ]; then
        ok "cmd_prompt_relevant: passed OR-joined query to search_extractions"
      else
        fail "cmd_prompt_relevant (query shape)" "captured='$CAPTURED'"
      fi
    else
      fail "cmd_prompt_relevant" "search_extractions never called"
    fi
    assert_envelope_or_empty "cmd_prompt_relevant (sentence)" "$OUT" "UserPromptSubmit"
  fi

  # Short prompt "hi" should still short-circuit (the < 10 chars check beats us
  # to the tokenizer in the shell hook).
  rm -f "$CAPTURE"
  set +e
  OUT="$(printf '%s' '{"session_id":"s","cwd":"/tmp/x","hook_event_name":"UserPromptSubmit","prompt":"hi"}' \
    | env PYTHONPATH="$STUB_DIR" "$HOOK_DIR/user-prompt-retrieve.sh" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -z "${OUT// }" ] && [ ! -f "$CAPTURE" ]; then
    ok "cmd_prompt_relevant: 'hi' short-circuited before DB query"
  else
    fail "cmd_prompt_relevant ('hi')" "rc=$RC out='$OUT' capture_exists=$([ -f "$CAPTURE" ] && echo yes || echo no)"
  fi

  # All-stopwords-and-shorts prompt longer than 10 chars: reaches the python
  # layer but tokenizes to empty -> no DB call, no output.
  rm -f "$CAPTURE"
  set +e
  OUT="$(printf '%s' '{"session_id":"s","cwd":"/tmp/x","hook_event_name":"UserPromptSubmit","prompt":"what is it on a"}' \
    | env PYTHONPATH="$STUB_DIR" "$HOOK_DIR/user-prompt-retrieve.sh" 2>/dev/null)"
  RC=$?
  set -e
  if [ "$RC" -eq 0 ] && [ -z "${OUT// }" ] && [ ! -f "$CAPTURE" ]; then
    ok "cmd_prompt_relevant: all-stopwords prompt skips DB"
  else
    fail "cmd_prompt_relevant (all-stopwords)" "rc=$RC out='$OUT' capture_exists=$([ -f "$CAPTURE" ] && echo yes || echo no)"
  fi

  # Truncation: tiny --max-tokens cap should cut the markdown.
  set +e
  OUT="$(env PYTHONPATH="$STUB_DIR" python3 "$HOOK_DIR/lib/query.py" prompt-relevant \
            --prompt "what did we decide about provider-x" \
            --cwd /tmp/x --limit 3 --max-tokens 5 2>/dev/null)"
  RC=$?
  set -e
  # 5 tokens * 4 chars/token = 20-char cap; output must be <= 20 chars.
  if [ "$RC" -eq 0 ] && [ -n "${OUT// }" ] && [ "${#OUT}" -le 20 ]; then
    ok "cmd_prompt_relevant: --max-tokens=5 truncates to ${#OUT} chars (<=20)"
  else
    fail "cmd_prompt_relevant (truncation)" "rc=$RC len=${#OUT} out='$OUT'"
  fi

  rm -rf "$STUB_DIR"
  rm -f "$TMPDATA/total-recall/index.db"
else
  ok "cmd_prompt_relevant: python3 missing — skipping"
fi

# ---------------------------------------------------------------------------
# 7. recall::log_json: NDJSON helper writes parseable lines + hooks emit
#    one event each with the right name and a non-empty elapsed_ms.
# ---------------------------------------------------------------------------
echo "[7] recall::log_json NDJSON emission"

EVENTS_FILE="$TMPDATA/total-recall/logs/events.jsonl"

# 7a. Direct helper call writes a JSON line we can round-trip through jq.
rm -f "$EVENTS_FILE"
set +e
bash -c "source '$HOOK_DIR/lib/common.sh'; recall::log_json hook.test foo=bar baz=qux"
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  fail "log_json (helper)" "rc=$RC"
elif [ ! -s "$EVENTS_FILE" ]; then
  fail "log_json (helper)" "events.jsonl missing or empty"
else
  if command -v jq >/dev/null 2>&1; then
    LAST="$(tail -n 1 "$EVENTS_FILE")"
    if ! printf '%s' "$LAST" | jq -e . >/dev/null 2>&1; then
      fail "log_json (helper)" "not valid JSON: $LAST"
    else
      EVT="$(printf '%s' "$LAST" | jq -r '.event // empty')"
      FOO="$(printf '%s' "$LAST" | jq -r '.foo // empty')"
      BAZ="$(printf '%s' "$LAST" | jq -r '.baz // empty')"
      TS="$(printf '%s'  "$LAST" | jq -r '.ts // empty')"
      if [ "$EVT" = "hook.test" ] && [ "$FOO" = "bar" ] && [ "$BAZ" = "qux" ] && [ -n "$TS" ]; then
        ok "log_json: valid JSON with event+kv pairs+ts"
      else
        fail "log_json (helper)" "fields wrong: $LAST"
      fi
    fi
  else
    ok "log_json: jq missing — skipped JSON validation"
  fi
fi

# 7b. After running each hook once, events.jsonl gains a line whose .event
#     matches the expected hook-event name and .elapsed_ms is non-empty +
#     numeric.
for hook_pair in \
  "session-start-signpost:hook.session_start:{\"session_id\":\"t\",\"cwd\":\"/tmp/none\",\"hook_event_name\":\"SessionStart\",\"source\":\"startup\"}" \
  "user-prompt-retrieve:hook.user_prompt_submit:{\"session_id\":\"t\",\"cwd\":\"/tmp/none\",\"hook_event_name\":\"UserPromptSubmit\",\"prompt\":\"a long enough prompt about widgets\"}" \
  "stop-index:hook.stop:{\"session_id\":\"t\",\"cwd\":\"/tmp/none\",\"hook_event_name\":\"Stop\"}" \
  "post-compact-index:hook.post_compact:{\"session_id\":\"t\",\"cwd\":\"/tmp/none\",\"hook_event_name\":\"PostCompact\"}" ; do

  HOOK_NAME="${hook_pair%%:*}"
  REST="${hook_pair#*:}"
  EXPECTED_EVT="${REST%%:*}"
  INPUT="${REST#*:}"

  rm -f "$EVENTS_FILE"
  set +e
  printf '%s' "$INPUT" | "$HOOK_DIR/$HOOK_NAME.sh" >/dev/null 2>&1
  RC=$?
  set -e

  if [ "$RC" -ne 0 ]; then
    fail "$HOOK_NAME log_json" "hook exit $RC"
    continue
  fi
  if [ ! -s "$EVENTS_FILE" ]; then
    fail "$HOOK_NAME log_json" "events.jsonl missing/empty"
    continue
  fi
  if ! command -v jq >/dev/null 2>&1; then
    ok "$HOOK_NAME log_json: events.jsonl populated (jq missing, skipping deep check)"
    continue
  fi
  # Look for a line with matching .event in this hook's output (there may be
  # only one, but be defensive in case of trap-fires).
  MATCH="$(jq -c --arg e "$EXPECTED_EVT" 'select(.event==$e)' "$EVENTS_FILE" 2>/dev/null | tail -n 1)"
  if [ -z "$MATCH" ]; then
    fail "$HOOK_NAME log_json" "no line with event=$EXPECTED_EVT in $(cat "$EVENTS_FILE")"
    continue
  fi
  ELAPSED="$(printf '%s' "$MATCH" | jq -r '.elapsed_ms // empty')"
  if [ -z "$ELAPSED" ]; then
    fail "$HOOK_NAME log_json" "elapsed_ms missing from $MATCH"
    continue
  fi
  case "$ELAPSED" in
    ''|*[!0-9]*) fail "$HOOK_NAME log_json" "elapsed_ms not integer: $ELAPSED"; continue ;;
  esac
  ok "$HOOK_NAME log_json: event=$EXPECTED_EVT elapsed_ms=$ELAPSED"
done

# 7c. Fallback path: when jq is shadowed, the printf fallback should still
#     produce parseable JSON.
rm -f "$EVENTS_FILE"
set +e
PATH_SHADOW="$(mktemp -d)"
# Place a 'jq' shim that fails so command -v finds nothing? command -v looks
# at PATH executables; easier to just strip jq from PATH.
ORIG_PATH="$PATH"
# Build a PATH that excludes any dir containing jq.
NEW_PATH=""
IFS=:; for p in $PATH; do
  if [ -x "$p/jq" ]; then continue; fi
  NEW_PATH="${NEW_PATH:+$NEW_PATH:}$p"
done; unset IFS
PATH="$NEW_PATH" bash -c "source '$HOOK_DIR/lib/common.sh'; recall::log_json hook.fallback alpha=one"
RC=$?
PATH="$ORIG_PATH"
set -e
if [ "$RC" -eq 0 ] && [ -s "$EVENTS_FILE" ]; then
  if command -v jq >/dev/null 2>&1; then
    if jq -e '.event=="hook.fallback" and .alpha=="one"' "$EVENTS_FILE" >/dev/null 2>&1; then
      ok "log_json: jq-less fallback writes parseable JSON"
    else
      fail "log_json fallback" "JSON does not match expected shape: $(cat "$EVENTS_FILE")"
    fi
  else
    ok "log_json: jq-less fallback wrote a line (no jq to validate with)"
  fi
else
  fail "log_json fallback" "rc=$RC events_present=$([ -s "$EVENTS_FILE" ] && echo yes || echo no)"
fi
rm -rf "$PATH_SHADOW"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "passed: $PASS    failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
