"""Tests for :mod:`detector.escalation`.

Every check (regex, length heuristic, draft-side check) has at least one
dedicated test. State-band boundaries are pinned at risk = 1, 3, 5, 7, 8
so any future re-tuning of thresholds will fail loudly here.
"""

from __future__ import annotations

import pytest

from detector.escalation import (
    BANNED_DRAFT_PATTERNS,
    DEFAULT_SIGNATURE_TYPOS,
    assess_escalation,
)

# ---------------------------------------------------------------------------
# Individual triggers
# ---------------------------------------------------------------------------


def test_directive_flip_fires_on_leading_no():
    """A leading "no " should add 1 and mark directive_flip."""
    r = assess_escalation("no don't do that")
    assert "directive_flip" in r.triggers
    assert r.risk == 1
    assert r.state == "calm"
    assert r.recommended_action == "ship_as_is"


def test_repetition_callout_fires_on_i_told_you():
    """The "I told you" phrase should add 2 and mark repetition_callout."""
    r = assess_escalation("I told you to use port 8080")
    assert "repetition_callout" in r.triggers
    # +2 only; no other triggers in this string
    assert r.risk == 2
    assert r.state == "mild_correction"


def test_profanity_fires_alone_for_wtf():
    """`wtf` matches the profanity regex (no longer a default signature typo)."""
    r = assess_escalation("wtf is going on")
    assert "profanity" in r.triggers
    # +2 profanity only = 2
    assert r.risk == 2
    assert r.state == "mild_correction"


def test_profanity_and_typo_fires_together_for_known_typo():
    """`teh` is in DEFAULT_SIGNATURE_TYPOS; combining with profanity gives both triggers."""
    r = assess_escalation("wtf teh thing broke")
    assert "profanity" in r.triggers
    assert "typing_under_pressure" in r.triggers
    # +2 profanity, +1 typo = 3
    assert r.risk == 3
    assert r.state == "mild_correction"


def test_personal_insult_fires_heavy():
    """Personal insult is the heaviest single trigger at +4."""
    r = assess_escalation("you are being an idiot")
    assert "personal_insult" in r.triggers
    assert r.risk == 4
    assert r.state == "escalated"
    assert r.recommended_action == "trim_to_5_lines"


def test_turns_shrinking_requires_previous_user_and_half_length():
    """`turns_shrinking` fires only when last_user is < half of previous."""
    prev = "Here is a long and detailed explanation of what I want you to do next."
    last = "stop"  # 4 chars, well under half
    r = assess_escalation(last, previous_user=prev)
    assert "turns_shrinking" in r.triggers
    # "stop" also matches directive_flip (+1) → +1 turns_shrinking = 2
    assert r.risk == 2


def test_typing_under_pressure_uses_default_typos():
    """Any default signature typo should mark typing_under_pressure."""
    # "teh" is in DEFAULT_SIGNATURE_TYPOS, doesn't hit other regexes.
    assert "teh" in DEFAULT_SIGNATURE_TYPOS
    r = assess_escalation("teh thing is broken")
    assert "typing_under_pressure" in r.triggers
    assert r.risk == 1
    assert r.state == "calm"


# ---------------------------------------------------------------------------
# Draft-side checks
# ---------------------------------------------------------------------------


def test_verbosity_under_correction_needs_long_draft_and_existing_risk():
    """Verbosity penalty applies only when risk>=2 AND draft>120 words."""
    long_draft = " ".join(["word"] * 130)
    # "i told you" gives +2 (repetition_callout), pushing risk to threshold.
    r = assess_escalation("i told you already", draft_response=long_draft)
    assert "repetition_callout" in r.triggers
    assert "verbosity_under_correction" in r.triggers
    # +2 repetition, +2 verbosity = 4.
    assert r.risk == 4
    assert r.state == "escalated"


def test_verbosity_under_correction_with_typo():
    """Verbosity + typo stacks correctly for risk=5."""
    long_draft = " ".join(["word"] * 130)
    # "i told you" (+2) + teh (+1) + long_draft verbosity (+2) = 5
    r = assess_escalation("i told you teh answer", draft_response=long_draft)
    assert "repetition_callout" in r.triggers
    assert "verbosity_under_correction" in r.triggers
    assert "typing_under_pressure" in r.triggers
    assert r.risk == 5
    assert r.state == "escalated"


def test_banned_phrase_in_draft_detected_and_listed():
    """Banned phrases in the draft add 2 and are echoed back to caller."""
    draft = "You're right, let me try again with a different approach."
    r = assess_escalation("nope", draft_response=draft)
    assert "banned_phrase_in_draft" in r.triggers
    # Two banned patterns matched: "you're right" and "let me try again".
    assert len(r.banned_phrases_in_draft) >= 2
    # Sanity: patterns echoed are the regex strings, not the matched text.
    for pat in r.banned_phrases_in_draft:
        assert pat in BANNED_DRAFT_PATTERNS


# ---------------------------------------------------------------------------
# State / action boundary table (risk = 1, 3, 5, 7, 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, prev, expected_risk, expected_state, expected_action",
    [
        # risk = 1 → calm, ship_as_is. Plain directive_flip.
        ("no thanks", None, 1, "calm", "ship_as_is"),
        # risk = 3 → mild_correction, ship_as_is.
        # "stop" → directive_flip (+1); "i told you" → repetition_callout (+2)
        ("stop, i told you", None, 3, "mild_correction", "ship_as_is"),
        # risk = 5 → escalated, trim_to_5_lines.
        # personal_insult (+4) + directive_flip (+1)
        ("no you idiot", None, 5, "escalated", "trim_to_5_lines"),
        # risk = 6 → high_escalated, run_command_paste_output.
        # personal_insult (+4) + profanity (+2); "wtf" is no longer a default typo.
        ("wtf you idiot", None, 6, "high_escalated", "run_command_paste_output"),
        # risk = 8 → breaking_point, silence_then_act.
        # personal_insult (+4, "stupid") + profanity (+2, "shit")
        # + repetition_callout (+2, "again")
        ("you stupid piece of shit, again", None, 8, "breaking_point", "silence_then_act"),
    ],
)
def test_state_boundaries(text, prev, expected_risk, expected_state, expected_action):
    r = assess_escalation(text, previous_user=prev)
    assert r.risk == expected_risk, (r.risk, r.triggers)
    assert r.state == expected_state
    assert r.recommended_action == expected_action
