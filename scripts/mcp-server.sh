#!/usr/bin/env bash
# mcp-server.sh — launch the total-recall stdio MCP server, resolving uv robustly.
#
# WHY THIS EXISTS: .mcp.json used to set "command": "uv" directly. That assumes uv
# is on the PATH Claude Code spawns MCP servers with — which on a fresh machine it
# often is NOT (uv not installed, or installed under ~/.local/bin off the spawn
# PATH, or system python too old). The result was a SILENT failure: the plugin
# installed and its hooks worked (they bootstrap uv via recall::uv), but the MCP
# server "Failed to connect" with no explanation, so recall never worked.
#
# This launcher resolves uv exactly like the hooks/CLI do (recall::uv in
# hooks/lib/common.sh: $RECALL_UV -> PATH -> bootstrapped data-dir uv -> one-time
# auto-install), then provisions a >=3.10 interpreter via uv and runs the server.
# uv progress goes to stderr; stdout stays a clean JSON-RPC stream.
set -euo pipefail

# 1. Plugin root: harness injects $CLAUDE_PLUGIN_ROOT; else walk up from scripts/.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT"
else
  PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
fi

# 2. Resolve uv via the canonical resolver (source common.sh), inline-fallback if
#    sourcing is unsafe. Mirrors scripts/recall-cli.sh exactly.
COMMON_SH="${PLUGIN_ROOT}/hooks/lib/common.sh"
if [ -f "$COMMON_SH" ]; then
  # Do NOT invent CLAUDE_PLUGIN_DATA=~/.claude/plugins/data — that is the
  # multi-plugin *parent*, not this plugin's data dir, and forces a second
  # empty …/data/total-recall/ index. Harness sets CLAUDE_PLUGIN_DATA correctly;
  # without it, recall::data_root discovers the real install DB.
  # shellcheck source=../hooks/lib/common.sh
  source "$COMMON_SH"
  UV="$(recall::uv)" || {
    echo "total-recall MCP: could not resolve uv — install uv (https://docs.astral.sh/uv/) or set RECALL_UV" >&2
    exit 1
  }
else
  # ---- inline fallback (common.sh absent) --------------------------------- #
  # inline: same discovery as recall::data_root (common.sh absent)
  if [ -n "${TOTAL_RECALL_DB_DIR:-}" ]; then
    RECALL_DATA_ROOT="${TOTAL_RECALL_DB_DIR}"
  elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    RECALL_DATA_ROOT="${CLAUDE_PLUGIN_DATA%/}/total-recall"
  else
    RECALL_DATA_ROOT=""
    best_sz=0
    for d in "${HOME}/.claude/plugins/data"/total-recall*/total-recall; do
      [ -f "${d}/index.db" ] || continue
      sz=$(stat -c%s "${d}/index.db" 2>/dev/null || echo 0)
      if [ "${sz:-0}" -gt "${best_sz:-0}" ]; then
        RECALL_DATA_ROOT="$d"
        best_sz="$sz"
      fi
    done
    [ -n "$RECALL_DATA_ROOT" ] || RECALL_DATA_ROOT="${HOME}/.local/share/total-recall"
  fi
  _can_run_uv() { [ -x "$1" ] && "$1" --version >/dev/null 2>&1; }
  UV=""
  if [ -n "${RECALL_UV:-}" ] && _can_run_uv "$RECALL_UV"; then
    UV="$RECALL_UV"
  elif command -v uv >/dev/null 2>&1 && _can_run_uv "$(command -v uv)"; then
    UV="$(command -v uv)"
  elif _can_run_uv "${HOME}/.local/bin/uv"; then
    UV="${HOME}/.local/bin/uv"
  elif _can_run_uv "${RECALL_DATA_ROOT}/bin/uv"; then
    UV="${RECALL_DATA_ROOT}/bin/uv"
  elif command -v curl >/dev/null 2>&1; then
    dest_bin="${RECALL_DATA_ROOT}/bin"; mkdir -p "$dest_bin"
    UV_INSTALL_DIR="$dest_bin" UV_UNMANAGED_INSTALL=1 \
      sh -c "curl -LsSf https://astral.sh/uv/install.sh | sh" >/dev/null 2>&1 || true
    _can_run_uv "${dest_bin}/uv" && UV="${dest_bin}/uv"
  fi
  if [ -z "$UV" ]; then
    echo "total-recall MCP: could not resolve uv — install uv (https://docs.astral.sh/uv/) or set RECALL_UV" >&2
    exit 1
  fi
fi

# 3. Run the stdio MCP server with a uv-provisioned >=3.10 interpreter.
cd "$PLUGIN_ROOT"
exec "$UV" run --project "$PLUGIN_ROOT" --python ">=3.10" python -m mcp_server "$@"
