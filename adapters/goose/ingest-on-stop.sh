#!/usr/bin/env bash
# Goose Stop / SessionEnd. Ingest the one session named in the payload.
#
# Goose has no PreCompact, PostCompact, or additionalContext. Stdout is the
# decision channel: Stop can block on {"decision":"block"}. This script
# prints nothing and always exits 0.
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
sid=""
if command -v jq >/dev/null 2>&1; then
  sid="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)"
fi
if [ -z "$sid" ]; then
  exit 0
fi

# Newest marketplace checkout that contains this package.
# TOTAL_RECALL_PY wins. Otherwise uv run --project that checkout, same as the hooks.
recall_project() {
  local c
  for c in "${TOTAL_RECALL_ROOT:-}" "${CLAUDE_PLUGIN_ROOT:-}"; do
    if [ -n "$c" ] && [ -f "$c/pyproject.toml" ]; then
      printf '%s' "$c"
      return 0
    fi
  done
  local best="" best_m=0 d m
  shopt -s nullglob
  for d in "${HOME}/.claude/plugins/cache/88plug/total-recall"/*; do
    [ -f "$d/pyproject.toml" ] || continue
    m="$(stat -c %Y "$d" 2>/dev/null || echo 0)"
    if [ "${m:-0}" -ge "$best_m" ]; then
      best="$d"
      best_m="${m:-0}"
    fi
  done
  shopt -u nullglob
  [ -n "$best" ] || return 1
  printf '%s' "$best"
}

run_index() {
  local -a db_prefix=()
  if [ -n "${TOTAL_RECALL_DB:-}" ]; then
    db_prefix=(--db "$TOTAL_RECALL_DB")
  elif [ -n "${TOTAL_RECALL_DB_DIR:-}" ]; then
    db_prefix=(--db "${TOTAL_RECALL_DB_DIR%/}/index.db")
  fi
  local -a cmd
  if [ -n "${TOTAL_RECALL_PY:-}" ]; then
    cmd=("$TOTAL_RECALL_PY" -m total_recall "${db_prefix[@]}" index --source goose --session "$sid")
  else
    local root=""
    root="$(recall_project 2>/dev/null || true)"
    if [ -n "$root" ] && command -v uv >/dev/null 2>&1; then
      cmd=(uv run --project "$root" --python ">=3.10" python -m total_recall
        "${db_prefix[@]}" index --source goose --session "$sid")
    else
      cmd=(python3 -m total_recall "${db_prefix[@]}" index --source goose --session "$sid")
    fi
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 12s "${cmd[@]}" >/dev/null 2>&1 || true
  else
    "${cmd[@]}" >/dev/null 2>&1 || true
  fi
}

run_index
exit 0
