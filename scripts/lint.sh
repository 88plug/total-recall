#!/usr/bin/env bash
set -euo pipefail
ruff check . --fix
ruff format .
# mypy total_recall lib extractors index vec mcp_server   # uncomment when types stabilize
