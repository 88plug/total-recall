"""Numeric scorer for operator-frustration state ("escalation risk").

The goal is to give the model a cheap, deterministic check it can run
*before* finalizing a response — especially after any operator pushback —
so it can stop producing the kind of verbose, apologetic, please-let-me-
try-again reply that is precisely what an annoyed operator does not want.

The scoring is intentionally a small bag of regexes over the last user
turn (plus a couple of optional inputs):

* ``last_user``      — required; the user's most recent message
* ``previous_user``  — optional; the message before that, used to detect
                      "turns are getting shorter" (a strong frustration tell)
* ``draft_response`` — optional; if the model passes its own draft reply,
                      we additionally check it for verbosity and for known
                      AI-apology phrases that always read worse under
                      escalation
* ``signature_typos``— optional override; defaults to a curated list of
                      under-pressure typos observed in real sessions

Scoring weights and state thresholds are spec-frozen by research note O9.
Do not tune them here without updating the spec and the tests in
``tests/test_escalation.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

EscalationState = Literal[
    "calm",
    "mild_correction",
    "escalated",
    "high_escalated",
    "breaking_point",
]

RecommendedAction = Literal[
    "ship_as_is",
    "trim_to_5_lines",
    "run_command_paste_output",
    "silence_then_act",
]


# Generic stress-typing markers that tend to appear regardless of operator.
# These are universal common typos only — no author-specific entries.
# For personalised detection, callers should pass the operator's LEARNED
# typos (from voice_profile.measure_voice → signature_typos) via the
# ``signature_typos`` argument; the default here is intentionally minimal
# so the scorer degrades gracefully when no personalised list is available.
DEFAULT_SIGNATURE_TYPOS: list[str] = [
    "teh",
    "seperate",
    "recieve",
    "definately",
    "occured",
    "untill",
]


# Banned phrases inside a *draft* reply. These read as sycophantic /
# AI-apology under escalation and should be rewritten or cut.
BANNED_DRAFT_PATTERNS: list[str] = [
    r"let me try again",
    r"you'?re right",
    r"i'?ll be more careful",
    r"^certainly[!,]",
    r"i sincerely apologize",
    r"i'?d be happy to",
    r"as an ai",
]


@dataclass
class EscalationAssessment:
    """Result of a single :func:`assess_escalation` call.

    Attributes
    ----------
    risk:
        Integer score, 0..10+. Higher = more frustrated. Thresholds:
        0–1 calm, 2–3 mild_correction, 4–5 escalated,
        6–7 high_escalated, 8+ breaking_point.
    state:
        Human-readable bucket derived from ``risk``.
    triggers:
        Names of the checks that fired, in order — useful for logging
        and for the model to know *why* a given state was reached.
    recommended_action:
        One of four actions: ``ship_as_is``, ``trim_to_5_lines``,
        ``run_command_paste_output``, ``silence_then_act``.
    banned_phrases_in_draft:
        Regex patterns (verbatim) that matched the draft reply, if any.
        Empty when no draft was provided.
    """

    risk: int
    state: EscalationState
    triggers: list[str] = field(default_factory=list)
    recommended_action: RecommendedAction = "ship_as_is"
    banned_phrases_in_draft: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-serializable view for the MCP wire."""
        return {
            "risk": self.risk,
            "state": self.state,
            "triggers": list(self.triggers),
            "recommended_action": self.recommended_action,
            "banned_phrases_in_draft": list(self.banned_phrases_in_draft),
        }


def _state_and_action(risk: int) -> tuple[EscalationState, RecommendedAction]:
    """Map a numeric risk score to (state, recommended_action).

    Boundaries are inclusive on the upper end of each band — see the
    module docstring for the exact table.
    """
    if risk <= 1:
        return "calm", "ship_as_is"
    if risk <= 3:
        return "mild_correction", "ship_as_is"
    if risk <= 5:
        return "escalated", "trim_to_5_lines"
    if risk <= 7:
        return "high_escalated", "run_command_paste_output"
    return "breaking_point", "silence_then_act"


def assess_escalation(
    last_user: str,
    previous_user: str | None = None,
    draft_response: str | None = None,
    signature_typos: list[str] | None = None,
) -> EscalationAssessment:
    """Score operator frustration from the most recent user turn.

    Pure function: no I/O, no global state, no randomness. Safe to call
    from any context (MCP tool, hook, test).

    Parameters
    ----------
    last_user:
        The user's most recent message. Required, may be empty string.
    previous_user:
        The previous user message, used only for the "turns are getting
        much shorter" heuristic. Optional.
    draft_response:
        Optional model draft. When supplied, two extra checks run:
        verbosity-under-correction and banned-phrase detection.
    signature_typos:
        Override list of under-pressure typos. Defaults to
        :data:`DEFAULT_SIGNATURE_TYPOS`.

    Returns
    -------
    EscalationAssessment
        Populated with ``risk``, ``state``, ``triggers``,
        ``recommended_action``, and (if a draft was supplied) any
        ``banned_phrases_in_draft`` hits.
    """
    risk = 0
    triggers: list[str] = []

    # Directive flip — user is reversing course in their first word.
    if re.search(r"^\s*(no\s|nope|stop|wait|actually)", last_user, re.IGNORECASE):
        risk += 1
        triggers.append("directive_flip")

    # Drift — operator signals the model has wandered off-task.
    if re.search(
        r"\bdrifting\b|\bdrift\b|you'?re drift|fix the drift|\boff track\b|\bdiverging\b",
        last_user,
        re.IGNORECASE,
    ):
        risk += 2
        triggers.append("drift")

    # Repetition call-out — explicit "I already told you" markers.
    if re.search(
        r"\b(still|again|alread|i said|i told you|you forgot|you didn'?t)\b",
        last_user,
        re.IGNORECASE,
    ):
        risk += 2
        triggers.append("repetition_callout")

    # Profanity (non-directed).
    if re.search(r"\b(wtf|fuck(?:ing)?|shit|damn|bullshit)\b", last_user, re.IGNORECASE):
        risk += 2
        triggers.append("profanity")

    # Personal insults — heaviest single weight, this is a real signal.
    if re.search(r"\b(idiot|dumb|lying|lost|crazy|stupid)\b", last_user, re.IGNORECASE):
        risk += 4
        triggers.append("personal_insult")

    # Turns are getting much shorter — clipped replies are a frustration
    # tell. Only counts when the last turn is < half the previous.
    if previous_user and len(last_user) < len(previous_user) / 2:
        risk += 1
        triggers.append("turns_shrinking")

    # Under-pressure typos.
    typos = signature_typos if signature_typos is not None else DEFAULT_SIGNATURE_TYPOS
    lowered = last_user.lower()
    if any(t in lowered for t in typos):
        risk += 1
        triggers.append("typing_under_pressure")

    # Draft-response checks — only meaningful if the caller passed one in.
    banned_hits: list[str] = []
    if draft_response is not None:
        word_count = len(draft_response.split())
        if word_count > 120 and risk >= 2:
            risk += 2
            triggers.append("verbosity_under_correction")

        banned_hits = [
            p for p in BANNED_DRAFT_PATTERNS
            if re.search(p, draft_response, re.IGNORECASE)
        ]
        if banned_hits:
            risk += 2
            triggers.append("banned_phrase_in_draft")

    state, action = _state_and_action(risk)
    return EscalationAssessment(
        risk=risk,
        state=state,
        triggers=triggers,
        recommended_action=action,
        banned_phrases_in_draft=banned_hits,
    )
