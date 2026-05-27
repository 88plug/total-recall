#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --user -e ".[vec,dev]"
echo "✓ installed; launch Claude Code with:"
echo "    claude --plugin-dir $(pwd)"
echo "Or add to settings.json plugins.localPaths."
