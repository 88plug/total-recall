---
description: Show total-recall metrics summary — sessions, tokens, top corrections, busiest project, longest session — over a window.
argument-hint: [since (e.g. 7d, 30d) — defaults to 7d]
---

Run `total-recall metrics summary --since ${ARGUMENTS:-7d}` via Bash.

Present the output to the user verbatim. If the user asks for trends across windows, run with different `--since` values (7d, 30d, 90d) and compare.

Common follow-ups:
- "Which sessions cost the most?" → `total-recall metrics sessions --top 5 --by tokens`
- "What's my Anthropic cost?" → `/total-recall:recall-cost`
- "What topics am I correcting most?" → `/total-recall:recall-topics`
- "Is the index healthy?" → `/total-recall:recall-health`
