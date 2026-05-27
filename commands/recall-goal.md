---
description: Show the active project goal stack — status, age, blocker.
argument-hint: [project-slug, defaults to cwd]
---

Call `get_active_goal` with `cwd=$ARGUMENTS` (or the current working directory if $ARGUMENTS is empty) to show the top-of-stack goal, then call `list_goals` with the same cwd for the surrounding stack.

Present:
- Active goal: title, status, age in days, current blocker (if any).
- Stack: each prior goal one line — title · status · last touched.

If the active goal is older than 14 days with no progress markers, flag it as stale and suggest the operator either close it or update its status.
