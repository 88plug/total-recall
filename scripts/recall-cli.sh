#!/usr/bin/env bash
# recall-cli.sh — robust wrapper to invoke the total_recall CLI via uv.
#
# Slash commands must NOT rely on a global `total-recall` console script or a
# system `python`/`python3` — those may be absent, 2.7/3.6, or missing deps.
# This wrapper mirrors the uv-resolution logic in hooks/lib/common.sh exactly
# (sources it when possible; duplicates only the critical path when sourcing
# is not safe) so CLI and hook behaviour are always in sync.
#
# Resolution order (matches recall::uv in hooks/lib/common.sh):
#   1. $RECALL_UV env override
#   2. uv on PATH
#   3. $HOME/.local/bin/uv  (common user-level uv install)
#   4. ${CLAUDE_PLUGIN_DATA:-~/.claude/plugins/data}/total-recall/bin/uv
#      (bootstrapped by the hooks on first fire)
#   5. Auto-download via the official uv installer (one-time, ~3-5 s)
#
# Usage:
#   bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" <subcmd> [args...]
#   bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" --version

set -euo pipefail

# --------------------------------------------------------------------------- #
# 1. Resolve plugin root: harness injects $CLAUDE_PLUGIN_ROOT; fall back to   #
#    walking up two dirs from this script (scripts/ → repo root).              #
# --------------------------------------------------------------------------- #
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT"
else
  PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
fi

# --------------------------------------------------------------------------- #
# 2. Try to source common.sh so we get the full recall::uv resolver.           #
#    If sourcing is unsafe (e.g. different shell), fall through to inline code. #
# --------------------------------------------------------------------------- #
COMMON_SH="${PLUGIN_ROOT}/hooks/lib/common.sh"

if [ -f "$COMMON_SH" ]; then
  # common.sh uses RECALL_DATA_ROOT which needs CLAUDE_PLUGIN_DATA or HOME.
  # We set a safe default so sourcing it never fails due to unbound vars.
  CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/plugins/data}"
  export CLAUDE_PLUGIN_DATA
  # shellcheck source=../hooks/lib/common.sh
  source "$COMMON_SH"

  UV="$(recall::uv)" || {
    echo "error: could not resolve uv — install uv (https://docs.astral.sh/uv/) or set RECALL_UV" >&2
    exit 1
  }
else
  # ---- inline fallback (common.sh absent) --------------------------------- #
  RECALL_DATA_ROOT="${CLAUDE_PLUGIN_DATA:-${HOME}/.claude/plugins/data}/total-recall"

  _can_run_uv() { [ -x "$1" ] && "$1" --version >/dev/null 2>&1; }

  UV=""
  # 1. Explicit override.
  if [ -n "${RECALL_UV:-}" ] && _can_run_uv "$RECALL_UV"; then
    UV="$RECALL_UV"
  # 2. PATH.
  elif command -v uv >/dev/null 2>&1 && _can_run_uv "$(command -v uv)"; then
    UV="$(command -v uv)"
  # 3. Common user-level install location.
  elif _can_run_uv "${HOME}/.local/bin/uv"; then
    UV="${HOME}/.local/bin/uv"
  # 4. Hooks-bootstrapped binary.
  elif _can_run_uv "${RECALL_DATA_ROOT}/bin/uv"; then
    UV="${RECALL_DATA_ROOT}/bin/uv"
  # 5. Auto-download (one-time).
  elif command -v curl >/dev/null 2>&1; then
    dest_bin="${RECALL_DATA_ROOT}/bin"
    mkdir -p "$dest_bin"
    UV_INSTALL_DIR="$dest_bin" UV_UNMANAGED_INSTALL=1 \
      sh -c "curl -LsSf https://astral.sh/uv/install.sh | sh" >/dev/null 2>&1
    if _can_run_uv "${dest_bin}/uv"; then
      UV="${dest_bin}/uv"
    fi
  fi

  if [ -z "$UV" ]; then
    echo "error: could not resolve uv — install uv (https://docs.astral.sh/uv/) or set RECALL_UV" >&2
    exit 1
  fi
fi

# --------------------------------------------------------------------------- #
# 3. cd to plugin root so pyproject.toml is found by `uv run --project`.      #
# --------------------------------------------------------------------------- #
cd "$PLUGIN_ROOT"

# --------------------------------------------------------------------------- #
# 4. Exec: uv run --project <root> --python >=3.10 python -m total_recall ...  #
#    Pass all args through; preserve exit code.                                 #
# --------------------------------------------------------------------------- #
exec "$UV" run --project "$PLUGIN_ROOT" --python ">=3.10" python -m total_recall "$@"
