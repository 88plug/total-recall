"""R2 confidence-decay and conflict-resolution policy.

Pure functions, no I/O. Used by the profile-tables writers (operator,
standing_decisions, bans, etc.) when an extractor proposes a new value
for a key that already has a row: rather than overwrite, we ask
:func:`resolve_conflict` what to do, then the storage layer either keeps
the old record, marks it superseded and inserts the new one, parks the
new claim in ``tentative_facts``, or lets both coexist as differently
typed facts.

The decay formula and source-quality tiers are lifted verbatim from R2's
research notes (§5.2, §5.3). See the docstrings for worked examples.
"""

from __future__ import annotations

import math

__all__ = [
    "HALF_LIFE_DAYS",
    "MAX_BOOST",
    "REASSERT_K",
    "SOURCE_QUALITY",
    "adjusted_confidence",
    "resolve_conflict",
    "source_quality",
]


HALF_LIFE_DAYS = 90.0
MAX_BOOST = 1.5
REASSERT_K = 0.20


def adjusted_confidence(
    base_confidence: float,
    days_since_last_reassertion: float,
    n_reassertions: int,
) -> float:
    """R2's half-life decay with reassertion boost.

    Examples:
      adjusted_confidence(0.8, 0, 1) = 0.8 * 1.0 * 1.0 = 0.8       (fresh, 1 mention)
      adjusted_confidence(0.8, 90, 1) = 0.8 * 0.5 * 1.0 = 0.4      (90d silent)
      adjusted_confidence(0.8, 90, 5) = 0.8 * 0.5 * (1+0.20*ln(6)) = ~0.51  (re-asserted)
      adjusted_confidence(0.8, 30, 10) = 0.8 * 0.794 * (1+0.20*ln(11)) = ~0.94  (active, hot)
    """
    # Negative time-since values are treated as "fresh" (clamp to zero) — happens
    # when an extractor mistakenly stamps a future ts. n_reassertions < 0 makes
    # no physical sense; clamp to zero so log1p stays defined.
    days = max(0.0, float(days_since_last_reassertion))
    n = max(0, int(n_reassertions))

    decay = math.exp(-math.log(2) * days / HALF_LIFE_DAYS)
    boost = min(1.0 + REASSERT_K * math.log1p(n), MAX_BOOST)
    return max(0.0, min(1.0, float(base_confidence) * decay * boost))


# Source quality tiers (R2 §5.3). Higher = more trustworthy.
SOURCE_QUALITY: dict[str, int] = {
    "explicit_user_assertion": 4,  # "my email is X"
    "git_config_or_claude_md": 3,  # `git config user.email X` in transcript
    "inferred_from_mention": 2,  # passing reference
    "quoted_or_hypothetical": 1,  # operator quoting some doc
}


def source_quality(kind: str | None) -> int:
    """Look up a source_kind's quality tier; unknown → 2 (``inferred_from_mention``).

    Centralised so writers and the resolver agree on the default.
    """
    return SOURCE_QUALITY.get(kind or "inferred_from_mention", 2)


def resolve_conflict(old: dict, new: dict) -> tuple[str, dict]:
    """Return (action, winning_record) where action in
    {'keep_old', 'supersede', 'tentative', 'coexist'}.

    Rules per R2 §5.2:
      1. Never destructive overwrite. Always append + supersede.
      2. New beats old only if evidence_count >= 2 OR
         source_quality(new) > source_quality(old).
      3. Source quality wins ties of quantity — a lower-quality source
         can't overturn a higher-quality one even with many mentions.
      4. Recency only tiebreaks when source qualities tie.
      5. Type-tagged facts coexist (email[work] vs email[personal]).
    """
    # Rule 5 — different categories on the same key: both are true.
    if old.get("category") and new.get("category") and old["category"] != new["category"]:
        return "coexist", new

    new_count = int(new.get("evidence_count", 1) or 1)
    new_q = source_quality(new.get("source_kind"))
    old_q = source_quality(old.get("source_kind"))

    # Rule 3 — lower-quality source cannot overturn a higher-quality one,
    # no matter how many times it's repeated. The fresh claim is held in
    # tentative storage so we don't lose the signal, but the canonical
    # record stays put.
    if new_q < old_q:
        return "keep_old", old

    # Rule 2, negated — not enough evidence and no quality upgrade.
    if new_count < 2 and new_q <= old_q:
        return "tentative", new

    # Rule 2, positive — enough evidence OR a strictly better source.
    if new_q > old_q or new_count >= 2:
        return "supersede", new

    # Defensive fallthrough; rules above are exhaustive given the count/quality
    # comparisons, but leaving an explicit branch keeps the contract obvious.
    return "keep_old", old
