---
description: Surface recent operator corrections on a topic — what Claude got wrong, what was demanded.
argument-hint: [topic]
---

If $ARGUMENTS is non-empty, call `recall_corrections_about` with `topic="$ARGUMENTS"` and `limit=10`. Otherwise call `get_recent_corrections` with the current working directory and `limit=5`.

For each correction present: date · cwd · one-line verbatim quote of the operator's correction · the model behavior that was being corrected.

If the same topic appears 3+ times, surface it as a "standing rule" candidate and suggest `/total-recall:recall-promote <topic>` to lift it into project auto-memory.
