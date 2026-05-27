---
description: List most-extracted topics from past Claude Code sessions — corrections, decisions, recurring preferences.
argument-hint: [since (default 30d)]
---

Run `total-recall metrics topics --since ${ARGUMENTS:-30d} --limit 15` via Bash.

For each topic returned, present:
- the topic phrase
- the count + which kinds (correction / decision / domain_fact)
- one example excerpt

If a correction topic has count ≥ 3, surface it as a "standing rule" — the user is repeatedly correcting Claude on this, and it's a candidate for `/total-recall:recall-promote` into project auto-memory.
