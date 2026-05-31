---
description: Show total-recall index health — DB size, session count, last ingest, top topics for this cwd.
---

Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" --json stats` via Bash and present:
- Total messages indexed, extractions per kind.
- Last ingest timestamp + age.
- Top 5 cwds by message volume.
- For current cwd, top 5 topics (via `prior_sessions_for_cwd` MCP or parse stats).

If DB missing, suggest: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" index --full` (warn: first-time backfill on ~776MB JSONL).
