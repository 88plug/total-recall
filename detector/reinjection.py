"""Decision function for re-injecting memory context on UserPromptSubmit.

Companion to ``detector.escalation``. Where escalation scores *frustration*,
this module answers a different question: given the current session state
and the latest user turn, should the hook push a fresh slice of memory
context back into the model's window *right now*?

The cost model is asymmetric. Injecting unnecessarily wastes tokens and
risks evicting useful context the model already has. Failing to inject
when needed makes the model repeat questions, forget operator preferences,
or re-litigate decisions — exactly the failures that produced the
``RECALL_PHRASES`` regex below.

Calibration target (research note R5, fit on the operator's own corpus):

* risk >= 3 (soft tier baseline): ~9.9% fire rate
* risk >= 5 (hard tier): ~2.5% fire rate
* risk >= 8 (extreme): ~0.4% fire rate

The function is therefore conservative by default: one *strong* signal
fires, but *weak* signals must combine. Suppression signals short-circuit
to avoid double-pushes inside the 5-minute Anthropic prompt-cache TTL.

Spec-frozen by R5. Do not tune thresholds here without updating both the
research note and ``tests/test_reinjection.py``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


@dataclass
class SessionState:
    cwd: str
    prev_cwd: str | None
    last_inject_ts: float                   # unix seconds of last memory push (0 if never)
    turns_since_inject: int
    post_compact: bool                      # PostCompact hook fired this session, not yet consumed
    project_known: bool                     # cwd present in total-recall index
    last_assistant_was_tool_heavy: bool     # >50% of last response was tool_use blocks
    silence_seconds: float                  # gap between last assistant turn and this user turn
    same_topic_streak: int                  # consecutive turns on same goal/scope
    escalation_risk: int                    # from detector.escalation (0-10)
    escalation_triggers: list[str] = field(default_factory=list)


CACHE_FRESH_SECONDS = 300       # 5-min Anthropic TTL
HARD_RISK = 5
SOFT_RISK = 3
MAX_INJECTIONS_PER_SESSION = 6  # R4 hard cap

RECALL_PHRASES = re.compile(
    r"\b(remember when|we (?:already|just)|didn'?t (?:we|you)|like (?:i|you) said|"
    r"as (?:i|you) (?:mentioned|said)|last time|previously)\b",
    re.I,
)


def should_reinject(
    state: SessionState,
    last_user: str,
    draft_response: str | None = None,
    injections_count: int = 0,
) -> tuple[bool, list[str]]:
    """Decide whether to re-inject memory context this turn.

    Conservative by default — combinations of weak signals OR one strong
    signal. Returns ``(decision, reasons)`` where ``reasons`` is a list of
    tagged strings suitable for logging and for auditing fire-rate
    calibration against R5.
    """
    now = time.time()

    # Session budget cap — R4 hard cap, never exceeded.
    if injections_count >= MAX_INJECTIONS_PER_SESSION:
        return False, ["suppress:session_cap_reached"]

    # Suppression signals — short-circuit unless a hard trigger overrides.
    # We evaluate the hard triggers first (in a separate pass) so that
    # explicit recall phrases / repetition callouts / post-compact still
    # fire even inside the cache-fresh window.
    hard_reasons: list[str] = []
    if state.escalation_risk >= HARD_RISK:
        hard_reasons.append(f"hard:escalation_risk={state.escalation_risk}")
    if "repetition_callout" in state.escalation_triggers:
        hard_reasons.append("hard:repetition_callout")
    if state.post_compact and state.project_known:
        hard_reasons.append("hard:post_compact_known_project")
    if RECALL_PHRASES.search(last_user):
        hard_reasons.append("hard:explicit_recall_phrase")
    if state.prev_cwd and state.cwd != state.prev_cwd and state.project_known:
        hard_reasons.append("hard:scope_shift_to_known_project")

    if hard_reasons:
        return True, hard_reasons

    # No hard trigger — now suppression signals apply.
    if now - state.last_inject_ts < CACHE_FRESH_SECONDS and state.turns_since_inject <= 2:
        return False, ["suppress:cache_fresh"]
    if state.same_topic_streak >= 4 and state.escalation_risk <= 1:
        return False, ["suppress:steady_state"]
    if state.last_assistant_was_tool_heavy and state.escalation_risk <= 2:
        return False, ["suppress:tool_loop"]

    # Soft triggers — need >= 2 to fire.
    soft: list[str] = []
    if state.escalation_risk >= SOFT_RISK:
        soft.append(f"soft:escalation_risk={state.escalation_risk}")
    if state.silence_seconds > 600 and state.turns_since_inject > 5:
        soft.append("soft:long_silence_resume")
    if state.turns_since_inject > 20:
        soft.append("soft:stale")
    if draft_response and len(draft_response.split()) > 200 and state.escalation_risk >= 2:
        soft.append("soft:verbose_under_correction")

    if len(soft) >= 2:
        return True, soft
    return False, soft or ["no_signal"]
