---
description: Show total-recall index health — last ingest age, hook fire rate, p95 latency, error count, DB size.
---

Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" metrics health` via Bash.

Interpret the output:
- `last_ingest_age_seconds` > 3600 → suggest running `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" index` manually; the Stop/PostCompact hooks may not be firing.
- `error_count_7d` > 0 → suggest `tail -50 ${CLAUDE_PLUGIN_DATA}/total-recall/logs/hooks.log`.
- `db_size_bytes` > 1 GB → suggest running `bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" rebuild` to compact, or `VACUUM`.
- `p95_ingest_ms` > 30000 (30s) → SessionStart hook may be timing out; investigate slow .jsonl files.

If everything looks healthy, say so briefly and don't repeat the table.
