"""Unit tests for :mod:`index.decay`.

The decay formula has three knobs (``HALF_LIFE_DAYS``, ``MAX_BOOST``,
``REASSERT_K``) and we want regression coverage on the actual arithmetic
— not just "function returned a float". Each pair below is hand-computed
from the closed-form expression in ``adjusted_confidence``'s docstring.

The conflict resolver gets one test per branch
(``keep_old``, ``supersede``, ``tentative``, ``coexist``).
"""

from __future__ import annotations

import math

import pytest

from index.decay import (
    HALF_LIFE_DAYS,
    MAX_BOOST,
    REASSERT_K,
    SOURCE_QUALITY,
    adjusted_confidence,
    resolve_conflict,
    source_quality,
)


def _expected(base: float, days: float, n: int) -> float:
    """Recompute the formula independently of the implementation."""
    decay = math.exp(-math.log(2) * days / HALF_LIFE_DAYS)
    boost = min(1.0 + REASSERT_K * math.log1p(n), MAX_BOOST)
    return max(0.0, min(1.0, base * decay * boost))


# ---------------------------------------------------------------------------
# adjusted_confidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base,days,n",
    [
        (0.8, 0, 0),  # fresh, no reassertions → boost = 1.0
        (0.8, 90, 1),  # exactly one half-life elapsed
        (0.8, 90, 5),  # decayed but re-asserted five times
        (0.8, 30, 10),  # active and hot
    ],
)
def test_adjusted_confidence_matches_formula(base, days, n):
    got = adjusted_confidence(base, days, n)
    want = _expected(base, days, n)
    assert got == pytest.approx(want, rel=1e-9, abs=1e-12)


def test_adjusted_confidence_fresh_no_reassertions_is_base():
    # n_reassertions=0 → log1p(0)=0 → boost=1.0 → result == base.
    assert adjusted_confidence(0.8, 0, 0) == pytest.approx(0.8)


def test_adjusted_confidence_half_life_is_half():
    # After exactly one half-life with no boost (n=0), value should halve.
    got = adjusted_confidence(1.0, HALF_LIFE_DAYS, 0)
    assert got == pytest.approx(0.5, rel=1e-9)


def test_adjusted_confidence_boost_clamps_at_max():
    # With n large enough that the raw boost would exceed MAX_BOOST,
    # the returned value should reflect the clamp (capped at 1.0 overall).
    got = adjusted_confidence(0.5, 0, 10**6)
    # 0.5 * 1.0 * 1.5 = 0.75 — well under the 1.0 ceiling, so we see the clamp.
    assert got == pytest.approx(0.5 * MAX_BOOST, rel=1e-9)


def test_adjusted_confidence_result_clamped_to_unit():
    # Even a high base with high boost shouldn't exceed 1.0.
    got = adjusted_confidence(0.9, 0, 10**6)
    assert 0.0 <= got <= 1.0
    assert got == pytest.approx(1.0)


def test_adjusted_confidence_handles_negative_inputs_defensively():
    # Negative days (clock skew) is treated as fresh, not blown up.
    got = adjusted_confidence(0.8, -10, 1)
    want = _expected(0.8, 0, 1)
    assert got == pytest.approx(want)


# ---------------------------------------------------------------------------
# source_quality lookup
# ---------------------------------------------------------------------------


def test_source_quality_known_tiers():
    assert source_quality("explicit_user_assertion") == 4
    assert source_quality("git_config_or_claude_md") == 3
    assert source_quality("inferred_from_mention") == 2
    assert source_quality("quoted_or_hypothetical") == 1


def test_source_quality_unknown_defaults_to_inferred():
    assert source_quality("something_made_up") == 2
    assert source_quality(None) == 2


def test_source_quality_table_is_strictly_ordered():
    # Sanity: the four named tiers form a strict 1..4 ladder.
    tiers = sorted(SOURCE_QUALITY.values())
    assert tiers == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# resolve_conflict — one test per branch
# ---------------------------------------------------------------------------


def test_resolve_conflict_coexist_when_categories_differ():
    # email[work] and email[personal] are both true simultaneously.
    old = {
        "value": "andrew@work.example",
        "category": "work",
        "source_kind": "explicit_user_assertion",
        "evidence_count": 3,
    }
    new = {
        "value": "andrew@personal.example",
        "category": "personal",
        "source_kind": "explicit_user_assertion",
        "evidence_count": 1,
    }
    action, winner = resolve_conflict(old, new)
    assert action == "coexist"
    assert winner is new


def test_resolve_conflict_tentative_when_lone_mention_no_quality_edge():
    # A single passing mention with the same source quality as the existing
    # record can't overturn it — it should be parked as tentative.
    old = {
        "value": "v1",
        "source_kind": "inferred_from_mention",
        "evidence_count": 5,
    }
    new = {
        "value": "v2",
        "source_kind": "inferred_from_mention",
        "evidence_count": 1,
    }
    action, winner = resolve_conflict(old, new)
    assert action == "tentative"
    assert winner is new


def test_resolve_conflict_supersede_when_evidence_threshold_met():
    # Two mentions at equal quality is enough to overturn (rule 2).
    old = {
        "value": "v1",
        "source_kind": "inferred_from_mention",
        "evidence_count": 1,
    }
    new = {
        "value": "v2",
        "source_kind": "inferred_from_mention",
        "evidence_count": 2,
    }
    action, winner = resolve_conflict(old, new)
    assert action == "supersede"
    assert winner is new


def test_resolve_conflict_supersede_on_strictly_better_source():
    # A single high-quality source (explicit assertion) beats an inferred one
    # even without crossing the evidence threshold.
    old = {
        "value": "v1",
        "source_kind": "inferred_from_mention",
        "evidence_count": 10,
    }
    new = {
        "value": "v2",
        "source_kind": "explicit_user_assertion",
        "evidence_count": 1,
    }
    action, winner = resolve_conflict(old, new)
    assert action == "supersede"
    assert winner is new


def test_resolve_conflict_keep_old_when_new_is_lower_quality():
    # A "quoted_or_hypothetical" mention — even repeated — can't overturn
    # an explicit user assertion. Rule 3: quality wins over quantity.
    old = {
        "value": "v1",
        "source_kind": "explicit_user_assertion",
        "evidence_count": 1,
    }
    new = {
        "value": "v2",
        "source_kind": "quoted_or_hypothetical",
        "evidence_count": 99,
    }
    action, winner = resolve_conflict(old, new)
    assert action == "keep_old"
    assert winner is old


def test_resolve_conflict_defaults_missing_source_kind_to_inferred():
    # When source_kind is omitted on both sides, both default to "inferred"
    # quality and the evidence threshold drives the decision.
    old = {"value": "v1", "evidence_count": 1}
    new = {"value": "v2", "evidence_count": 1}
    action, _ = resolve_conflict(old, new)
    assert action == "tentative"  # single mention, no quality edge


def test_resolve_conflict_only_one_side_categorised_does_not_coexist():
    # Coexist requires *both* records to carry a non-empty category.
    old = {"value": "v1", "category": "work", "source_kind": "explicit_user_assertion"}
    new = {"value": "v2", "source_kind": "explicit_user_assertion", "evidence_count": 2}
    action, _ = resolve_conflict(old, new)
    assert action == "supersede"
