"""Outcome tracking for re-injection decisions — closing the feedback loop.

A re-injection is "useful" iff it actually prevented a correction in the
next 3 operator turns. "Useless" iff a ``repetition_callout`` or
``personal_insult`` fired in that window despite the push.

We model per-trigger precision with a Beta(useful+1, useless+1) posterior
(Bayes-Laplace add-one smoothing). With N>=50 fires we have enough signal
to recommend tightening (low precision → too many false positives) or
loosening (high precision but rare → we are leaving useful pushes on the
table). Recommendations are advisory; the operator decides whether to
edit thresholds in :mod:`detector.reinjection`.

Schema is created idempotently on first use so callers can pass any
already-open SQLite connection (the main index DB or an in-memory test
DB).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reinjection_outcomes (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  triggers TEXT NOT NULL,             -- JSON list of trigger names
  injection_chars INTEGER,
  outcome TEXT,                       -- 'useful' | 'useless' | 'pending' (within 3 turns)
  evaluated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_outcomes_session ON reinjection_outcomes(session_id, turn);
CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON reinjection_outcomes(outcome);
"""


# How many turns we wait after the injection before judging it.
EVAL_WINDOW_TURNS = 3

# Triggers that mean the injection failed to prevent the failure mode.
NEGATIVE_TRIGGERS = ("repetition_callout", "personal_insult")

# Sample size required before we believe a per-trigger precision number.
MIN_FIRES_FOR_RECOMMENDATION = 50

# Decision boundaries on the posterior mean.
TIGHTEN_BELOW = 0.50
LOOSEN_ABOVE = 0.95


# --------------------------------------------------------------------------- #
# Schema bootstrap
# --------------------------------------------------------------------------- #


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ``reinjection_outcomes`` table + indexes if missing."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def record_injection(
    conn: sqlite3.Connection,
    session_id: str,
    turn: int,
    triggers: Iterable[str],
    injection_chars: int | None = None,
    *,
    ts: int | None = None,
) -> int:
    """Record a single re-injection event.

    Returns the row id. Starts as ``outcome='pending'`` — call
    :func:`evaluate_pending` after 3 more operator turns to resolve it.
    """
    ensure_schema(conn)
    triggers_list = list(triggers)
    row_ts = int(ts if ts is not None else time.time())
    cur = conn.execute(
        "INSERT INTO reinjection_outcomes "
        "(session_id, turn, ts, triggers, injection_chars, outcome, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', NULL)",
        (
            session_id,
            int(turn),
            row_ts,
            json.dumps(triggers_list),
            int(injection_chars) if injection_chars is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def evaluate_pending(
    conn: sqlite3.Connection,
    session_id: str,
    current_turn: int,
    escalation_in_next_n_turns: Callable[[str, int, int], bool],
) -> int:
    """Resolve any pending injections whose 3-turn window has elapsed.

    ``escalation_in_next_n_turns(session_id, start_turn, end_turn)`` must
    return ``True`` iff a ``repetition_callout`` or ``personal_insult``
    fired in any turn ``t`` with ``start_turn <= t <= end_turn``.

    Returns the number of rows freshly resolved (useful + useless).
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT id, turn FROM reinjection_outcomes "
        "WHERE session_id = ? AND outcome = 'pending' "
        "  AND ? - turn >= ? "
        "ORDER BY turn",
        (session_id, int(current_turn), EVAL_WINDOW_TURNS),
    ).fetchall()

    now = int(time.time())
    resolved = 0
    for row in rows:
        row_id, inj_turn = row[0], int(row[1])
        start = inj_turn + 1
        end = inj_turn + EVAL_WINDOW_TURNS
        bad = bool(escalation_in_next_n_turns(session_id, start, end))
        outcome = "useless" if bad else "useful"
        conn.execute(
            "UPDATE reinjection_outcomes SET outcome = ?, evaluated_at = ? WHERE id = ?",
            (outcome, now, row_id),
        )
        resolved += 1
    if resolved:
        conn.commit()
    return resolved


# --------------------------------------------------------------------------- #
# Precision (Beta posterior)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TriggerStats:
    trigger: str
    useful: int
    useless: int
    precision: float  # Beta(useful+1, useless+1) posterior mean
    n: int  # useful + useless

    @property
    def recommend(self) -> str:
        if self.n < MIN_FIRES_FOR_RECOMMENDATION:
            return "keep"
        if self.precision < TIGHTEN_BELOW:
            return "tighten"
        if self.precision > LOOSEN_ABOVE:
            return "loosen"
        return "keep"


def trigger_precision(conn: sqlite3.Connection, trigger_name: str) -> tuple[float, int, int]:
    """Beta(useful+1, useless+1) posterior mean for ``trigger_name``.

    Returns ``(precision, useful_count, useless_count)``. A trigger that
    has never fired returns ``(0.5, 0, 0)`` (uniform Beta(1,1) prior).
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT triggers, outcome FROM reinjection_outcomes WHERE outcome IN ('useful', 'useless')"
    ).fetchall()
    useful = 0
    useless = 0
    for triggers_json, outcome in rows:
        try:
            triggers = json.loads(triggers_json)
        except (TypeError, ValueError):
            continue
        if trigger_name not in triggers:
            continue
        if outcome == "useful":
            useful += 1
        elif outcome == "useless":
            useless += 1
    precision = (useful + 1) / (useful + useless + 2)
    return precision, useful, useless


def _all_triggers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT triggers FROM reinjection_outcomes WHERE outcome IN ('useful', 'useless')"
    ).fetchall()
    out: set[str] = set()
    for (triggers_json,) in rows:
        try:
            for t in json.loads(triggers_json):
                if isinstance(t, str):
                    out.add(t)
        except (TypeError, ValueError):
            continue
    return out


def all_trigger_stats(conn: sqlite3.Connection) -> list[TriggerStats]:
    """Return :class:`TriggerStats` for every trigger we've observed.

    Sorted by ``n`` descending so the most-fired triggers come first;
    ties broken by trigger name for stable output.
    """
    ensure_schema(conn)
    out: list[TriggerStats] = []
    for trig in _all_triggers(conn):
        precision, useful, useless = trigger_precision(conn, trig)
        out.append(
            TriggerStats(
                trigger=trig,
                useful=useful,
                useless=useless,
                precision=precision,
                n=useful + useless,
            )
        )
    out.sort(key=lambda s: (-s.n, s.trigger))
    return out


def recommend_threshold_adjustment(conn: sqlite3.Connection) -> dict[str, str]:
    """Per-trigger recommendation: ``'tighten' | 'loosen' | 'keep'``.

    Only triggers with at least :data:`MIN_FIRES_FOR_RECOMMENDATION`
    resolved fires get a non-``'keep'`` recommendation. The rest are
    reported as ``'keep'`` (i.e. we lack the evidence to change them).
    """
    return {s.trigger: s.recommend for s in all_trigger_stats(conn)}
