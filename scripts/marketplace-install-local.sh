#!/usr/bin/env bash
# Test the plugin as if it were installed via the marketplace,
# but using a local path instead of a github clone.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "✓ plugin root: $(pwd)"
echo "Launch Claude Code with:"
echo "    claude --plugin-dir $(pwd)"
echo ""
echo "To verify hooks fire correctly, watch the log:"
echo "    tail -f \${CLAUDE_PLUGIN_DATA:-~/.claude/plugins/data}/total-recall/logs/hooks.log"
