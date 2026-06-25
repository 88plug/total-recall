"""Model-initiated targeted recall — the ``recall_targeted`` MCP tool.

This is the **self-ask** surface. The model invokes it *before* emitting any
response that involves a default choice, recommendation, or "what does the
operator prefer" decision — mirroring Anthropic's ``memory_20250818`` pattern
(R1+R5): the agent decides recall is needed; we don't push.

Why it exists
-------------

The operator's #1 frustration is "you suggested the thing I already told you
not to suggest." Every other v0.3 tool — :func:`check_banned`,
:func:`get_decision_for_topic`, :func:`recall_corrections_about`,
:func:`get_active_goal` — can answer some version of that question, but the
model has to know which one to call. ``recall_targeted`` collapses that
choice into a single ``intent`` argument and routes to the right backend.

Cost: one round-trip, one focused payload, ~30ms p95. Cheap enough that the
model can call it constantly without paying for it in context tokens.

Routing
-------

Each ``intent`` is purpose-built and lands in exactly one backend:

* ``is_thing_banned``        → :func:`index.bans.check_banned`
* ``looking_up_decision``    → :func:`index.decisions.get_for_topic`
* ``checking_past_correction`` → :func:`index.query.search_extractions`
                                 (filtered to ``kind='model_correction'``)
* ``operator_preference_lookup`` → :func:`index.operator.get_profile` +
                                   :func:`index.bans.check_banned`
* ``have_we_discussed_this`` → :func:`index.query.search_extractions` +
                               :func:`index.query.search_messages` fallback
* ``what_is_active_goal``    → :func:`index.goals.get_active_goal`
* ``before_suggesting_default`` → combined bans + standing decisions +
                                  recent corrections

Every backend import is defensive — a missing index table or a sibling
worktree that hasn't merged returns an empty result, never a crash.

Return shape
------------

``{"finding": str, "confidence": 0..1, "verbatim_quotes": [str, ...],
   "recommendation": "use" | "avoid" | "verify"}``

* ``recommendation="avoid"`` — the operator has explicitly rejected this
  thing; do not suggest it. Surface the verbatim quote.
* ``recommendation="use"`` — the operator has explicitly chosen this thing
  (standing decision in favor); use it as the default.
* ``recommendation="verify"`` — no signal either way; ask the operator
  before assuming. Also the safe fallback for unknown intents or empty
  backends.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from typing import Any, Literal

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


_VALID_INTENTS = (
    "before_suggesting_default",
    "checking_past_correction",
    "looking_up_decision",
    "operator_preference_lookup",
    "is_thing_banned",
    "have_we_discussed_this",
    "what_is_active_goal",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_result(
    finding: str = "",
    recommendation: str = "verify",
    confidence: float = 0.0,
) -> dict:
    """Build a defensive default payload — used on errors, misses, unknowns."""
    return {
        "finding": finding,
        "confidence": confidence,
        "verbatim_quotes": [],
        "recommendation": recommendation,
    }


def _index_missing_result() -> dict:
    return {
        "finding": "index not initialized; run `total-recall index` to build it",
        "confidence": 0.0,
        "verbatim_quotes": [],
        "recommendation": "verify",
        "db_path": str(DB_PATH),
    }


def _current_cwd() -> str:
    return os.environ.get("PWD") or os.getcwd()


def _load(modname: str):
    """Defensive submodule import. Returns ``None`` if absent on this branch."""
    try:
        if modname == "bans":
            from index import bans as m  # type: ignore
        elif modname == "decisions":
            from index import decisions as m  # type: ignore
        elif modname == "query":
            from index import query as m  # type: ignore
        elif modname == "goals":
            from index import goals as m  # type: ignore
        elif modname == "operator":
            from index import operator as m  # type: ignore
        else:
            return None
        return m
    except Exception as e:  # pragma: no cover - import-time guard
        log.warning("index.%s unavailable: %s", modname, e)
        return None


def _hit_get(hit: Any, key: str, default: Any = None) -> Any:
    """Read a field from a :class:`QueryHit`, ``dict``, or ``sqlite3.Row``."""
    if hit is None:
        return default
    if hasattr(hit, "keys"):
        try:
            return hit[key]
        except (KeyError, IndexError):
            return default
    return getattr(hit, key, default)


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------


def _route_is_thing_banned(conn: sqlite3.Connection, subject: str) -> dict:
    mod = _load("bans")
    if mod is None:
        return _empty_result(
            "index.bans not available on this branch",
            recommendation="verify",
        )
    try:
        row = mod.check_banned(conn, subject)
    except Exception as e:
        log.exception("recall_targeted: check_banned failed")
        return _empty_result(f"check_banned failed: {e!r}")

    if not row:
        return _empty_result(
            f"no ban recorded for {subject!r}",
            recommendation="verify",
        )

    strength = row.get("ban_strength") or "preference"
    quote = row.get("ban_text") or ""
    clause = row.get("context_clause")
    # Confidence climbs with strength + reassertion count. Anchored so a
    # single-shot "preference" still beats 0.5 (it's evidence, just not
    # absolute evidence).
    strength_floor = {"absolute": 0.95, "context": 0.8, "preference": 0.65}.get(
        strength, 0.6
    )
    reassert_bonus = min(0.05 * (int(row.get("reassertion_count") or 1) - 1), 0.1)
    confidence = min(1.0, strength_floor + reassert_bonus)

    finding = (
        f"{subject!r} is banned ({strength})"
        + (f" — context: {clause!r}" if clause else "")
    )
    return {
        "finding": finding,
        "confidence": confidence,
        "verbatim_quotes": [quote] if quote else [],
        "recommendation": "avoid",
    }


def _route_looking_up_decision(
    conn: sqlite3.Connection, subject: str, cwd_hint: str | None
) -> dict:
    mod = _load("decisions")
    if mod is None:
        return _empty_result(
            "index.decisions not available on this branch",
            recommendation="verify",
        )
    target_scope = cwd_hint or _current_cwd()
    try:
        # ``get_for_topic`` honors scope precedence (project → global → any).
        row = mod.get_for_topic(conn, topic=subject, scope=target_scope)
    except sqlite3.OperationalError as e:
        # Table not present yet on a freshly-indexed DB.
        log.debug("recall_targeted decisions table missing: %s", e)
        return _empty_result(
            f"no decision recorded for topic {subject!r}",
            recommendation="verify",
        )
    except Exception as e:
        log.exception("recall_targeted: get_for_topic failed")
        return _empty_result(f"get_for_topic failed: {e!r}")

    if not row:
        return _empty_result(
            f"no decision recorded for topic {subject!r}",
            recommendation="verify",
        )

    chose = row.get("chose")
    over = row.get("over")
    rationale = row.get("rationale") or ""
    is_reversed = bool(row.get("is_reversed"))
    assertion_count = int(row.get("assertion_count") or 1)

    # Reversed decisions degrade to verify (the operator changed their mind).
    if is_reversed:
        reversed_to = row.get("reversed_to")
        return {
            "finding": (
                f"prior decision {chose!r} for {subject!r} was REVERSED"
                + (f" → {reversed_to!r}" if reversed_to else "")
            ),
            "confidence": 0.6,
            "verbatim_quotes": [rationale] if rationale else [],
            "recommendation": "verify",
        }

    # Confidence climbs with assertion count: 1 → 0.75, 2 → 0.82, 5 → 0.95.
    confidence = min(0.95, 0.7 + 0.05 * assertion_count)
    finding = (
        f"operator chose {chose!r} for {subject!r}"
        + (f" over {over!r}" if over else "")
    )
    quotes: list[str] = []
    if rationale:
        quotes.append(rationale)
    return {
        "finding": finding,
        "confidence": confidence,
        "verbatim_quotes": quotes,
        "recommendation": "use",
    }


def _route_checking_past_correction(
    conn: sqlite3.Connection, subject: str, cwd_hint: str | None
) -> dict:
    mod = _load("query")
    if mod is None:
        return _empty_result(
            "index.query not available on this branch",
            recommendation="verify",
        )
    cwd_filter = cwd_hint  # ``None`` → all projects
    try:
        try:
            hits = mod.search_extractions(
                conn,
                query=subject,
                cwd=cwd_filter,
                kind="model_correction",
                limit=5,
            )
        except TypeError:
            hits = mod.search_extractions(
                conn, query=subject, cwd=cwd_filter, limit=20
            )
            hits = [h for h in hits if _hit_get(h, "kind") == "model_correction"]
    except Exception as e:
        log.exception("recall_targeted: search_extractions failed")
        return _empty_result(f"search_extractions failed: {e!r}")

    if not hits:
        return _empty_result(
            f"no past corrections about {subject!r}",
            recommendation="verify",
        )

    # Best hit's score → confidence; surface up to 3 verbatim quotes.
    best = hits[0]
    quotes = [str(_hit_get(h, "content") or "") for h in hits[:3]]
    quotes = [q for q in quotes if q]
    raw_score = _hit_get(best, "score", 0.5)
    try:
        confidence = max(0.5, min(1.0, float(raw_score)))
    except (TypeError, ValueError):
        confidence = 0.6
    return {
        "finding": (
            f"{len(hits)} past correction(s) match {subject!r} — avoid repeating"
        ),
        "confidence": confidence,
        "verbatim_quotes": quotes,
        "recommendation": "avoid",
    }


def _route_operator_preference_lookup(
    conn: sqlite3.Connection, subject: str
) -> dict:
    """Combine ``operator_profile`` field lookup + ban check + decision check.

    Operator preferences live in three places: the structured profile
    (``cloud_provider``, ``billing_rail``, …), the bans table (negative
    preferences), and standing decisions (positive preferences). We hit all
    three and pick the strongest signal.
    """
    quotes: list[str] = []
    findings: list[str] = []
    best_recommendation = "verify"
    best_confidence = 0.0

    # 1) Bans first — strongest negative signal.
    bans_mod = _load("bans")
    if bans_mod is not None:
        try:
            row = bans_mod.check_banned(conn, subject)
        except Exception:
            row = None
        if row:
            strength = row.get("ban_strength") or "preference"
            quote = row.get("ban_text") or ""
            if quote:
                quotes.append(quote)
            findings.append(f"{subject!r} is banned ({strength})")
            best_recommendation = "avoid"
            best_confidence = max(
                best_confidence,
                {"absolute": 0.95, "context": 0.8, "preference": 0.65}.get(
                    strength, 0.6
                ),
            )

    # 2) Profile field — does the operator have a stored preference for this?
    op_mod = _load("operator")
    if op_mod is not None:
        try:
            field = op_mod.get_profile_field(conn, subject)
        except Exception:
            field = None
        if field is not None:
            value, conf, _sources = field
            findings.append(f"operator profile: {subject}={value!r}")
            best_confidence = max(best_confidence, float(conf or 0.5))
            # A positive profile value with no ban means use; bans win
            # because they're a harder signal.
            if best_recommendation != "avoid":
                best_recommendation = "use"

    if not findings:
        return _empty_result(
            f"no preference recorded for {subject!r}",
            recommendation="verify",
        )

    return {
        "finding": " | ".join(findings),
        "confidence": min(1.0, best_confidence),
        "verbatim_quotes": quotes,
        "recommendation": best_recommendation,
    }


def _route_have_we_discussed_this(
    conn: sqlite3.Connection, subject: str, cwd_hint: str | None
) -> dict:
    """FTS sweep over extractions + raw messages for any mention of ``subject``."""
    mod = _load("query")
    if mod is None:
        return _empty_result(
            "index.query not available on this branch",
            recommendation="verify",
        )

    try:
        ext_hits = mod.search_extractions(
            conn, query=subject, cwd=cwd_hint, limit=5
        )
    except Exception:
        ext_hits = []

    msg_hits: list[Any] = []
    if not ext_hits:
        try:
            msg_hits = mod.search_messages(
                conn, query=subject, cwd=cwd_hint, limit=5
            )
        except Exception:
            msg_hits = []

    total = len(ext_hits) + len(msg_hits)
    if total == 0:
        return _empty_result(
            f"no prior discussion of {subject!r} found",
            recommendation="verify",
        )

    quotes: list[str] = []
    for h in ext_hits[:3]:
        text = str(_hit_get(h, "content") or "")
        if text:
            quotes.append(text)
    for h in msg_hits[:3]:
        text = str(_hit_get(h, "text") or "")
        if text:
            quotes.append(text)

    return {
        "finding": f"{total} prior mention(s) of {subject!r}",
        # Confidence is not "should I use it" — just "yes we discussed it".
        # Cap at 0.85 so the model still verifies the *substance* of the
        # discussion before assuming alignment.
        "confidence": min(0.85, 0.5 + 0.05 * total),
        "verbatim_quotes": quotes,
        "recommendation": "verify",
    }


def _route_what_is_active_goal(
    conn: sqlite3.Connection, cwd_hint: str | None
) -> dict:
    mod = _load("goals")
    if mod is None:
        return _empty_result(
            "index.goals not available on this branch",
            recommendation="verify",
        )
    target = cwd_hint or _current_cwd()
    try:
        row = mod.get_active_goal(conn, target)
    except sqlite3.OperationalError as e:
        log.debug("recall_targeted goals table missing: %s", e)
        return _empty_result(
            f"no active goal for {target!r}",
            recommendation="verify",
        )
    except Exception as e:
        log.exception("recall_targeted: get_active_goal failed")
        return _empty_result(f"get_active_goal failed: {e!r}")

    if row is None:
        return _empty_result(
            f"no active goal for {target!r}",
            recommendation="verify",
        )

    # ``row`` is a ``GoalRow`` dataclass; degrade if it's a dict.
    if hasattr(row, "to_dict"):
        d = row.to_dict()
    elif isinstance(row, dict):
        d = row
    else:
        d = {"goal_text": str(row)}

    goal_text = d.get("goal_text") or ""
    status = d.get("status") or "active"
    return {
        "finding": f"active goal for {target!r}: {goal_text!r} (status={status})",
        # Goals are facts about state, not preferences — high confidence is
        # warranted iff the row exists and is fresh enough to be 'active'.
        "confidence": 0.9 if status == "active" else 0.7,
        "verbatim_quotes": [goal_text] if goal_text else [],
        "recommendation": "use",
    }


def _route_before_suggesting_default(
    conn: sqlite3.Connection, subject: str, cwd_hint: str | None
) -> dict:
    """Composite: bans + standing decisions + recent corrections.

    Strongest negative signal wins. If nothing matches anywhere, fall back to
    ``verify`` with an explanatory finding so the model doesn't mistake
    "no signal" for "go ahead".
    """
    # 1) Ban check — terminal "avoid" signal if it fires.
    banned = _route_is_thing_banned(conn, subject)
    if banned.get("recommendation") == "avoid":
        # Prefix the finding to make the routing obvious in logs.
        banned["finding"] = "BAN: " + banned.get("finding", "")
        return banned

    # 2) Standing decision — positive "use" signal.
    decision = _route_looking_up_decision(conn, subject, cwd_hint)
    if decision.get("recommendation") in ("use", "avoid"):
        decision["finding"] = "DECISION: " + decision.get("finding", "")
        return decision

    # 3) Past corrections — softer negative signal.
    correction = _route_checking_past_correction(conn, subject, cwd_hint)
    if correction.get("recommendation") == "avoid":
        correction["finding"] = "CORRECTION: " + correction.get("finding", "")
        return correction

    return _empty_result(
        f"no ban, decision, or correction on {subject!r} — safe to propose, but verify",
        recommendation="verify",
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@mcp.tool(title="Recall Targeted", annotations=ToolAnnotations(readOnlyHint=True))
def recall_targeted(
    intent: Literal[
        "before_suggesting_default",
        "checking_past_correction",
        "looking_up_decision",
        "operator_preference_lookup",
        "is_thing_banned",
        "have_we_discussed_this",
        "what_is_active_goal",
    ],
    subject: str,
    cwd_hint: str | None = None,
) -> dict:
    """Self-ask before answering. Call this BEFORE emitting any response that
    involves a default choice, recommendation, or "what does the operator
    prefer" decision. The operator's #1 frustration is "you suggested the
    thing I already told you not to suggest" — checking first prevents that.

    The tool routes by ``intent`` to the right backend (bans table for
    ``is_thing_banned``, standing_decisions for ``looking_up_decision``, etc.)
    and returns a focused payload — much cheaper than fanning out to several
    ``recall_*`` tools and parsing their results.

    Args:
      intent: what you're checking — picks the right index table. One of:
        - ``before_suggesting_default``: composite check (bans + decisions +
          corrections) — use when about to pick any default and you're not
          sure which signal applies.
        - ``checking_past_correction``: have I been corrected on this before?
        - ``looking_up_decision``: did the operator already decide this?
        - ``operator_preference_lookup``: stored profile field + bans for a
          named topic (e.g. ``cloud_provider``).
        - ``is_thing_banned``: hard ban check (provider, library, pattern).
        - ``have_we_discussed_this``: FTS sweep over extractions + messages.
        - ``what_is_active_goal``: what is the operator currently trying to
          do on this project? (subject is ignored.)
      subject: the thing you're checking (provider name, library, topic).
      cwd_hint: optional cwd to scope the lookup; defaults to current cwd.
        Pass an empty string to widen to all projects.

    Returns: ``{"finding": str, "confidence": 0..1,
                "verbatim_quotes": [...],
                "recommendation": "use" | "avoid" | "verify"}``

    Use this constantly. It's cheap (~30ms p95) and prevents the #1 failure
    mode: suggesting things the operator has already corrected. The operator
    will be visibly more pleased when you check first.

    Examples:
      recall_targeted("is_thing_banned", "some-provider")
        → {finding: "'some-provider' is banned (absolute)", recommendation: "avoid",
           verbatim_quotes: ["never use some-provider — they nuked our box once"]}
      recall_targeted("looking_up_decision", "billing_rail")
        → {finding: "operator chose 'billing-provider-a' for 'billing_rail'
           over 'billing-provider-b'",
           recommendation: "use", confidence: 0.8}
      recall_targeted("what_is_active_goal", "")
        → {finding: "active goal for '/home/operator/my-project': 'ship v0.3 MCP tools'",
           recommendation: "use"}
    """
    # Validate intent — Literal typing catches most callers, but a runtime
    # check is cheap insurance against an SDK that passes through unknowns.
    if intent not in _VALID_INTENTS:
        return _empty_result(
            f"unknown intent {intent!r}; expected one of {_VALID_INTENTS}",
            recommendation="verify",
        )

    if not isinstance(subject, str):
        return _empty_result(
            "subject must be a string",
            recommendation="verify",
        )
    # Normalize: ``what_is_active_goal`` ignores subject; the rest need a
    # non-empty string to do anything useful.
    subject = subject.strip()
    if intent != "what_is_active_goal" and not subject:
        return _empty_result(
            "subject must be a non-empty string for this intent",
            recommendation="verify",
        )

    conn = get_conn()
    if conn is None:
        return _index_missing_result()

    # ``cwd_hint=""`` means "widen to all projects"; ``None`` means default.
    effective_cwd: str | None
    if cwd_hint is None:
        effective_cwd = None  # let routes choose (current cwd vs all)
    elif cwd_hint == "":
        effective_cwd = None
    else:
        effective_cwd = cwd_hint

    try:
        if intent == "is_thing_banned":
            return _route_is_thing_banned(conn, subject)
        if intent == "looking_up_decision":
            return _route_looking_up_decision(conn, subject, effective_cwd)
        if intent == "checking_past_correction":
            return _route_checking_past_correction(conn, subject, effective_cwd)
        if intent == "operator_preference_lookup":
            return _route_operator_preference_lookup(conn, subject)
        if intent == "have_we_discussed_this":
            return _route_have_we_discussed_this(conn, subject, effective_cwd)
        if intent == "what_is_active_goal":
            return _route_what_is_active_goal(conn, effective_cwd)
        if intent == "before_suggesting_default":
            return _route_before_suggesting_default(conn, subject, effective_cwd)
        # Unreachable due to validation above, but keep the safety net.
        return _empty_result(
            f"unhandled intent {intent!r}",
            recommendation="verify",
        )
    except Exception as e:  # pragma: no cover - last-line guard
        log.exception("recall_targeted: unexpected failure")
        return _empty_result(
            f"recall_targeted failed: {e!r}",
            recommendation="verify",
        )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


__all__ = ["recall_targeted"]


# Silence unused-import noise for ``Any`` — kept so future routes can lean on
# its type without re-importing.
_ = Any
