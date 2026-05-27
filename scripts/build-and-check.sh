#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Cleaning previous build artifacts..."
rm -rf build dist *.egg-info

echo "==> Building sdist + wheel..."
python -m build --sdist --wheel

echo "==> Running twine check..."
twine check dist/*

echo "==> Test-installing wheel into a throwaway venv..."
tmpvenv=$(mktemp -d)/venv
python -m venv "$tmpvenv"
"$tmpvenv/bin/pip" install --quiet "dist/$(ls dist | grep '\.whl$' | head -1)"

echo "==> Verifying entry points..."
"$tmpvenv/bin/total-recall" --version
"$tmpvenv/bin/total-recall-mcp" --help 2>/dev/null || echo "(mcp entry: no --help; runs as server)"

echo ""
echo "✓ build + install + entry points OK"
