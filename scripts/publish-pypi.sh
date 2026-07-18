#!/usr/bin/env bash
# Publish total-recall to PyPI with safety checks.
# Usage:
#   PYPI_TOKEN=pypi-... bash scripts/publish-pypi.sh
#   PYPI_TOKEN=pypi-... bash scripts/publish-pypi.sh --yes
set -euo pipefail
cd "$(dirname "$0")/.."

YES=0
for arg in "$@"; do
    [[ "$arg" == "--yes" ]] && YES=1
done

# ── Require dist/ artifacts ────────────────────────────────────────────────
if [[ ! -d dist ]] || [[ -z "$(ls dist/*.whl 2>/dev/null)" ]]; then
    echo "ERROR: No wheel found in dist/. Run scripts/build-and-check.sh first." >&2
    exit 1
fi

# ── Resolve token ──────────────────────────────────────────────────────────
TWINE_ARGS=()
if [[ -n "${PYPI_TOKEN:-}" ]]; then
    TWINE_ARGS+=(--username __token__ --password "$PYPI_TOKEN")
elif [[ -f "$HOME/.pypirc" ]]; then
    echo "==> Using credentials from ~/.pypirc"
else
    echo "ERROR: Set PYPI_TOKEN env var or configure ~/.pypirc with a PyPI token." >&2
    exit 1
fi

# ── Show what will be uploaded ─────────────────────────────────────────────
echo ""
echo "Artifacts to upload:"
ls -lh dist/
echo ""

# ── Confirm unless --yes ───────────────────────────────────────────────────
if [[ $YES -eq 0 ]]; then
    read -rp "Upload these artifacts to PyPI? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# ── Run twine check one more time ──────────────────────────────────────────
echo "==> Final twine check..."
twine check dist/*

# ── Upload ─────────────────────────────────────────────────────────────────
echo "==> Uploading to PyPI..."
twine upload "${TWINE_ARGS[@]}" dist/*

echo ""
echo "✓ Published successfully!"
echo ""
echo "Smoke test:"
echo "  uvx total-recall-mcp"
echo "  pip install total-recall"
echo "  ollama pull qwen3-embedding:0.6b   # hybrid dense recall"
