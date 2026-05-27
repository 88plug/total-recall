---
description: One-shot operator briefing for this cwd — identity, active goal, top decisions, bans, voice cheats, recent corrections.
---

Call the `get_operator_context` MCP tool with `cwd` set to the current working directory and print the response verbatim.

Use this to manually re-trigger the SessionStart signpost mid-session, or after a compaction wipes context. The payload already fits in the SessionStart budget, so do not truncate.

Common follow-ups:
- "Why is this goal blocked?" → `/total-recall:recall-goal`
- "What corrections am I hitting most?" → `/total-recall:recall-corrections`
- "Is provider X banned?" → `/total-recall:recall-check-banned <provider>`
