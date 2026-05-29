---
description: Inspect a single past session — print ai-title, message count, branches, top extractions.
argument-hint: <session-id>
---

`bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" inspect $ARGUMENTS --json --show-extractions` and present:
- ai-title and last-prompt (resume seed).
- Message/branch/sidechain counts.
- Top 5 extractions by score, grouped by kind.
- If the session includes an `away_summary` extraction, quote it verbatim.
