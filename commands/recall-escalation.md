---
description: Assess the operator's current frustration level from their last turn — calm/mild/escalated/high/breaking_point.
---

Call the `assess_escalation_risk` MCP tool, passing the operator's last user message as `last_user` and the draft assistant response (if one exists in scratch) as `draft_response`.

Report:
- Detected state (calm / mild / escalated / high / breaking_point).
- The trigger phrases that fired the classifier.
- Recommended action (proceed / soften tone / stop and ask / abort current plan).

Use proactively the moment you notice pushback signals (repeated "no", "stop", "wrong again", swearing). Do not wait for breaking_point.
