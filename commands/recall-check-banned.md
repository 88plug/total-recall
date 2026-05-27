---
description: Check if a provider, tool, or pattern is on the operator ban list before suggesting it.
argument-hint: <provider-tool-or-pattern>
---

Call the `check_banned` MCP tool with `thing="$ARGUMENTS"`.

Present the result: banned status, the reason from the originating correction, and the date it was banned. If `banned == true`, do not recommend $ARGUMENTS as a default — surface the ban explicitly and suggest the operator's preferred alternative (look it up via `/total-recall:recall-decisions $ARGUMENTS` if not in the ban record).

Run this before recommending any provider, library, or tooling default the operator has not endorsed in this session.
