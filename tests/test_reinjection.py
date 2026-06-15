"""Tests for ``detector.reinjection.should_reinject``.

Calibration target from R5:
* risk >= 3: ~9.9% fire rate
* risk >= 5: ~2.5% fire rate
* risk >= 8: ~0.4% fire rate

The decision function must be conservative: hard signals fire alone, soft
signals must combine, suppression short-circuits inside the prompt-cache
TTL — but a true callout (repetition_callout, explicit recall phrase,
etc.) overrides suppression because forgetting *those* is the failure
mode this system exists to prevent.
"""

from __future__ import annotations

import time

import pytest

from detector.reinjection import (
    MAX_INJECTIONS_PER_SESSION,
    SessionState,
    should_reinject,
)


def _baseline(**overrides) -> SessionState:
    """Neutral state — no signals, no suppression. Override per-test."""
    defaults = dict(
        cwd="/home/operator/project-a",
        prev_cwd="/home/operator/project-a",
        last_inject_ts=0.0,
        turns_since_inject=10,
        post_compact=False,
        project_known=True,
        last_assistant_was_tool_heavy=False,
        silence_seconds=5.0,
        same_topic_streak=1,
        escalation_risk=0,
        escalation_triggers=[],
    )
    defaults.update(overrides)
    return SessionState(**defaults)


# ---------------------------------------------------------------- hard triggers


def test_hard_escalation_risk_fires_alone():
    state = _baseline(escalation_risk=7)
    fire, reasons = should_reinject(state, "fine, whatever")
    assert fire is True
    assert any(r.startswith("hard:escalation_risk=") for r in reasons)


def test_hard_repetition_callout_fires_alone():
    state = _baseline(escalation_triggers=["repetition_callout"])
    fire, reasons = should_reinject(state, "you keep doing this")
    assert fire is True
    assert "hard:repetition_callout" in reasons


def test_hard_post_compact_known_project_fires():
    state = _baseline(post_compact=True, project_known=True)
    fire, reasons = should_reinject(state, "ok continue")
    assert fire is True
    assert "hard:post_compact_known_project" in reasons


def test_hard_explicit_recall_phrase_fires():
    state = _baseline()
    fire, reasons = should_reinject(state, "remember when we fixed the relay last week?")
    assert fire is True
    assert "hard:explicit_recall_phrase" in reasons


def test_hard_scope_shift_to_known_project_fires():
    state = _baseline(
        cwd="/home/operator/project-b",
        prev_cwd="/home/operator/project-a",
        project_known=True,
    )
    fire, reasons = should_reinject(state, "now look at this one")
    assert fire is True
    assert "hard:scope_shift_to_known_project" in reasons


# ----------------------------------------------------------- suppression positives


def test_suppress_cache_fresh():
    state = _baseline(
        last_inject_ts=time.time() - 30,  # 30s ago, well inside TTL
        turns_since_inject=1,
    )
    fire, reasons = should_reinject(state, "next step please")
    assert fire is False
    assert reasons == ["suppress:cache_fresh"]


def test_suppress_steady_state():
    state = _baseline(same_topic_streak=5, escalation_risk=1)
    fire, reasons = should_reinject(state, "ok next file")
    assert fire is False
    assert reasons == ["suppress:steady_state"]


def test_suppress_tool_loop():
    state = _baseline(last_assistant_was_tool_heavy=True, escalation_risk=1)
    fire, reasons = should_reinject(state, "continue")
    assert fire is False
    assert reasons == ["suppress:tool_loop"]


# --------------------------------------------------------------- soft combinations


def test_soft_two_signals_fires():
    # risk=3 (soft tier) + stale (turns_since_inject>20)
    state = _baseline(escalation_risk=3, turns_since_inject=25)
    fire, reasons = should_reinject(state, "doing it again huh")
    assert fire is True
    assert len(reasons) >= 2
    assert all(r.startswith("soft:") for r in reasons)


def test_soft_long_silence_plus_verbose_under_correction_fires():
    state = _baseline(
        escalation_risk=2,
        silence_seconds=900,         # >600s
        turns_since_inject=10,       # >5
    )
    long_draft = " ".join(["word"] * 250)  # >200 words
    fire, reasons = should_reinject(state, "back", draft_response=long_draft)
    assert fire is True
    assert "soft:long_silence_resume" in reasons
    assert "soft:verbose_under_correction" in reasons


# ----------------------------------------------------------- soft-single negative


def test_soft_single_signal_does_not_fire():
    # Only one soft signal: stale. No hard, no suppression. Must not fire.
    state = _baseline(turns_since_inject=25)
    fire, reasons = should_reinject(state, "ok")
    assert fire is False
    assert reasons == ["soft:stale"]


# ----------------------------------------------------------- session cap negative


def test_session_cap_blocks_even_hard_trigger():
    state = _baseline(escalation_risk=10, escalation_triggers=["repetition_callout"])
    fire, reasons = should_reinject(
        state,
        "remember when we said this?",
        injections_count=MAX_INJECTIONS_PER_SESSION,
    )
    assert fire is False
    assert reasons == ["suppress:session_cap_reached"]


# -------------------------------------------------------------------- edge cases


def test_edge_scope_shift_to_unknown_project_does_not_fire():
    state = _baseline(
        cwd="/tmp/scratch",
        prev_cwd="/home/operator/project-a",
        project_known=False,
    )
    fire, reasons = should_reinject(state, "look here")
    assert fire is False
    assert "hard:scope_shift_to_known_project" not in reasons


def test_edge_risk_4_alone_does_not_fire():
    # risk=4 is above SOFT_RISK (3) but below HARD_RISK (5). Alone, it's
    # one soft signal — must not fire without a second signal.
    state = _baseline(escalation_risk=4)
    fire, reasons = should_reinject(state, "ok")
    assert fire is False
    assert reasons == ["soft:escalation_risk=4"]


def test_edge_repetition_callout_overrides_cache_fresh():
    # Cache is fresh AND last inject was 1 turn ago — normally suppressed.
    # But repetition_callout is a hard signal that *must* override: it's
    # exactly the case where the model has already forgotten despite a
    # recent inject, so we push again.
    state = _baseline(
        last_inject_ts=time.time() - 30,
        turns_since_inject=1,
        escalation_triggers=["repetition_callout"],
    )
    fire, reasons = should_reinject(state, "you did this already")
    assert fire is True
    assert "hard:repetition_callout" in reasons


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
