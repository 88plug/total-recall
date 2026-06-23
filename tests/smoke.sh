#!/usr/bin/env bash
# Lightweight wiring smoke test — no Python deps required.
# Checks: manifest/hook JSON is valid, every hook + mcp script exists and
# parses under bash -n. Exit 0 on a clean repo.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== smoke: manifest JSON valid ==="
for f in .claude-plugin/plugin.json .mcp.json hooks/hooks.json marketplace-entry.json; do
    python3 -c "import json,sys; json.load(open('$f'))" && echo "  ok: $f"
done

echo "=== smoke: hook + mcp bash syntax ==="
while read -r f; do
    [ -n "$f" ] || continue
    bash -n "$f" && echo "  ok: $f"
done < <(find hooks scripts -name "*.sh" 2>/dev/null)

echo "=== smoke: all good ==="
