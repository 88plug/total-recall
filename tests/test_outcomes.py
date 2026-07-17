"""Tests for ``detector.outcomes``.

Covers:

* record_injection → starts as 'pending'
* evaluate_pending → resolves to 'useful' / 'useless' after the 3-turn window
* trigger_precision → Beta(useful+1, useless+1) posterior arithmetic
* recommend_threshold_adjustment → respects sample-size floor + boundaries
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from detector.outcomes import (
    EVAL_WINDOW_TURNS,
    MIN_FIRES_FOR_RECOMMENDATION,
    all_trigger_stats,
    ensure_schema,
    evaluate_pending,
    recommend_threshold_adjustment,
    record_injection,
    trigger_precision,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def test_record_injection_starts_pending(conn):
    row_id = record_injection(
        conn,
        session_id="s1",
        turn=5,
        triggers=["hard:repetition_callout"],
        injection_chars=512,
    )
    assert row_id > 0
    row = conn.execute(
        "SELECT session_id, turn, triggers, injection_chars, outcome, evaluated_at "
        "FROM reinjection_outcomes WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row[0] == "s1"
    assert row[1] == 5
    assert json.loads(row[2]) == ["hard:repetition_callout"]
    assert row[3] == 512
    assert row[4] == "pending"
    assert row[5] is None


# --------------------------------------------------------------------------- #
# Evaluation roundtrip
# --------------------------------------------------------------------------- #


def test_evaluate_marks_useful_when_no_escalation(conn):
    """Inject at turn 5; 3 turns elapse with no callout → 'useful'."""
    record_injection(conn, "s1", turn=5, triggers=["hard:repetition_callout"])

    def no_escalation(session_id, start_turn, end_turn):
        return False

    resolved = evaluate_pending(
        conn,
        session_id="s1",
        current_turn=5 + EVAL_WINDOW_TURNS,
        escalation_in_next_n_turns=no_escalation,
    )
    assert resolved == 1
    outcome = conn.execute(
        "SELECT outcome FROM reinjection_outcomes WHERE session_id='s1'"
    ).fetchone()[0]
    assert outcome == "useful"


def test_evaluate_marks_useless_when_escalation_in_window(conn):
    """Inject at turn 5; callout fires at turn 6 → 'useless'."""
    record_injection(conn, "s1", turn=5, triggers=["soft:long_silence_resume"])

    calls = []

    def escalation_in_turn_6(session_id, start_turn, end_turn):
        calls.append((session_id, start_turn, end_turn))
        # Window is turn 6..8; pretend turn 6 had a callout.
        return start_turn <= 6 <= end_turn

    resolved = evaluate_pending(
        conn,
        session_id="s1",
        current_turn=8,
        escalation_in_next_n_turns=escalation_in_turn_6,
    )
    assert resolved == 1
    outcome = conn.execute(
        "SELECT outcome FROM reinjection_outcomes WHERE session_id='s1'"
    ).fetchone()[0]
    assert outcome == "useless"
    # And the callback was given the right window.
    assert calls == [("s1", 6, 8)]


def test_evaluate_skips_when_window_not_yet_elapsed(conn):
    """Only 2 turns have elapsed → still pending, not yet resolved."""
    record_injection(conn, "s1", turn=5, triggers=["hard:explicit_recall_phrase"])

    resolved = evaluate_pending(
        conn,
        session_id="s1",
        current_turn=6,  # only 1 turn later
        escalation_in_next_n_turns=lambda *_: False,
    )
    assert resolved == 0
    outcome = conn.execute(
        "SELECT outcome FROM reinjection_outcomes WHERE session_id='s1'"
    ).fetchone()[0]
    assert outcome == "pending"


def test_evaluate_is_idempotent(conn):
    """Re-running after already-resolved rows does nothing more."""
    record_injection(conn, "s1", turn=5, triggers=["hard:repetition_callout"])
    evaluate_pending(conn, "s1", 8, lambda *_: False)
    resolved = evaluate_pending(conn, "s1", 8, lambda *_: True)
    assert resolved == 0  # already resolved, not re-judged


def test_evaluate_scoped_per_session(conn):
    """A different session's pending rows are not touched."""
    record_injection(conn, "s1", turn=5, triggers=["hard:repetition_callout"])
    record_injection(conn, "s2", turn=5, triggers=["hard:repetition_callout"])
    evaluate_pending(conn, "s1", 8, lambda *_: False)
    s1 = conn.execute("SELECT outcome FROM reinjection_outcomes WHERE session_id='s1'").fetchone()[
        0
    ]
    s2 = conn.execute("SELECT outcome FROM reinjection_outcomes WHERE session_id='s2'").fetchone()[
        0
    ]
    assert s1 == "useful"
    assert s2 == "pending"


# --------------------------------------------------------------------------- #
# Beta posterior arithmetic
# --------------------------------------------------------------------------- #


def _seed_outcomes(conn, trigger: str, useful: int, useless: int) -> None:
    """Insert pre-resolved rows directly to avoid full evaluate plumbing."""
    next_turn = [0]
    for _ in range(useful):
        next_turn[0] += 1
        conn.execute(
            "INSERT INTO reinjection_outcomes "
            "(session_id, turn, ts, triggers, injection_chars, outcome, evaluated_at) "
            "VALUES ('sX', ?, 0, ?, NULL, 'useful', 0)",
            (next_turn[0], json.dumps([trigger])),
        )
    for _ in range(useless):
        next_turn[0] += 1
        conn.execute(
            "INSERT INTO reinjection_outcomes "
            "(session_id, turn, ts, triggers, injection_chars, outcome, evaluated_at) "
            "VALUES ('sX', ?, 0, ?, NULL, 'useless', 0)",
            (next_turn[0], json.dumps([trigger])),
        )
    conn.commit()


def test_trigger_precision_beta_posterior(conn):
    """10 useful / 2 useless → (10+1)/(10+2+2) = 11/14 ≈ 0.786."""
    _seed_outcomes(conn, "hard:repetition_callout", useful=10, useless=2)
    precision, useful, useless = trigger_precision(conn, "hard:repetition_callout")
    assert useful == 10
    assert useless == 2
    assert precision == pytest.approx(11 / 14, abs=1e-6)
    assert 0.78 <= precision <= 0.80


def test_trigger_precision_unobserved_is_uniform_prior(conn):
    """Never-fired trigger → Beta(1,1) mean = 0.5."""
    precision, useful, useless = trigger_precision(conn, "soft:never_seen")
    assert useful == 0
    assert useless == 0
    assert precision == pytest.approx(0.5)


def test_trigger_precision_handles_co_fired_triggers(conn):
    """A row with multiple triggers contributes to each one's precision."""
    conn.execute(
        "INSERT INTO reinjection_outcomes "
        "(session_id, turn, ts, triggers, outcome) "
        "VALUES ('sX', 1, 0, ?, 'useful')",
        (json.dumps(["hard:repetition_callout", "soft:stale"]),),
    )
    conn.commit()
    p_rep, u_rep, _ = trigger_precision(conn, "hard:repetition_callout")
    p_stale, u_stale, _ = trigger_precision(conn, "soft:stale")
    assert u_rep == 1 and u_stale == 1
    assert p_rep == pytest.approx(2 / 3)
    assert p_stale == pytest.approx(2 / 3)


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #


def test_recommend_tighten_when_low_precision_and_enough_data(conn):
    """precision < 0.5 with N >= 50 → 'tighten'."""
    # 15 useful, 35 useless → posterior mean = 16/52 ≈ 0.308
    _seed_outcomes(conn, "soft:long_silence_resume", useful=15, useless=35)
    recs = recommend_threshold_adjustment(conn)
    assert recs["soft:long_silence_resume"] == "tighten"


def test_recommend_keep_when_sample_too_small(conn):
    """Even very bad precision is 'keep' below the sample-size floor."""
    _seed_outcomes(conn, "soft:flaky", useful=1, useless=10)
    recs = recommend_threshold_adjustment(conn)
    # With N=11 < MIN_FIRES_FOR_RECOMMENDATION (50), we have to 'keep'.
    assert MIN_FIRES_FOR_RECOMMENDATION == 50
    assert recs["soft:flaky"] == "keep"


def test_recommend_loosen_when_very_high_precision(conn):
    """precision > 0.95 with N >= 50 → 'loosen' (we are being too cautious)."""
    _seed_outcomes(conn, "hard:explicit_recall_phrase", useful=80, useless=1)
    recs = recommend_threshold_adjustment(conn)
    assert recs["hard:explicit_recall_phrase"] == "loosen"


def test_recommend_keep_at_mid_precision(conn):
    """precision in [0.5, 0.95] → 'keep' (boundary discipline)."""
    # 30 useful, 25 useless → posterior 31/57 ≈ 0.544
    _seed_outcomes(conn, "hard:escalation_risk=5", useful=30, useless=25)
    recs = recommend_threshold_adjustment(conn)
    assert recs["hard:escalation_risk=5"] == "keep"


def test_all_trigger_stats_sorted_by_volume(conn):
    """Most-fired trigger first."""
    _seed_outcomes(conn, "hard:repetition_callout", useful=20, useless=2)
    _seed_outcomes(conn, "soft:stale", useful=3, useless=2)
    stats = all_trigger_stats(conn)
    assert [s.trigger for s in stats] == [
        "hard:repetition_callout",
        "soft:stale",
    ]
    assert stats[0].n == 22
    assert stats[1].n == 5
