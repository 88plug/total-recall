---
description: Estimate Anthropic cost from total-recall's index — per-model token breakdown over a window.
argument-hint: [since (default 30d) and optional model overrides]
---

Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" metrics cost --since ${ARGUMENTS:-30d}` via Bash.

If the user supplied custom rates as args (e.g. `sonnet=3/15`), pass them through as `--rate` flags. Default rates are checked into `total_recall/cost.py` and may be stale — if the user mentions a recent Anthropic price change, suggest they override with `--rate`.

Present the breakdown as a compact table. If total cost is surprising (e.g. >$50/week), proactively suggest looking at `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" metrics sessions --by tokens --top 5` to find the heaviest sessions.
