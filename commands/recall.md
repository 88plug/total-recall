---
description: Search past Claude Code sessions for prior decisions, corrections, preferences, and progress on a topic.
argument-hint: <topic>
---

Use the `recall` MCP tool to search past sessions for: $ARGUMENTS

Steps:
1. Call `recall(topic="$ARGUMENTS", kind="any", scope="this_cwd", limit=8)`.
2. If 0 results in this_cwd, retry with `scope="all_projects"`.
3. Present results as a compact list: `<kind>` · `<cwd>` · `<date>` — `<one-line excerpt>`.
4. If a result looks load-bearing for current work, quote user's verbatim phrasing.
5. If nothing relevant, say so; do not invent.

Never surface raw `raw_json` blobs.
