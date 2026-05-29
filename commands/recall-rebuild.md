---
description: Rebuild the total-recall index from scratch (drops then full re-ingest). Destructive.
---

Summarize and confirm:
- Drops DB at `${CLAUDE_PLUGIN_DATA}/total-recall/index.db`.
- Re-ingests every `~/.claude/projects/<slug>/*.jsonl`.
- Estimated 1-5 minutes for ~1GB on a dev laptop.

On confirmation: run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" rebuild --yes` and tail output.
