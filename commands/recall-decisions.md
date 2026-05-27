---
description: Show standing decisions the operator has made (e.g. provider-a > provider-b, billing-a > billing-b). Useful before recommending defaults.
argument-hint: [topic]
---

If $ARGUMENTS is non-empty, call `get_decision_for_topic` with `topic="$ARGUMENTS"`. Otherwise call `list_standing_decisions` with `scope="global"`.

Present each decision as: `<chosen> > <rejected>` · date · one-line rationale from the originating session.

Run this before recommending any default that has plausible alternatives the operator has previously evaluated. Pair with `/total-recall:recall-check-banned` to confirm the rejected option is not also banned outright.
