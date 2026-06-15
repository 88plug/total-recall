"""Tests for the ``recall_targeted`` MCP tool (model-initiated self-ask).

The tool is purpose-built for the "model wonders if it's missing context"
pattern — it routes by ``intent`` to a single backend and returns a focused
``{finding, confidence, verbatim_quotes, recommendation}`` payload.

The tests below cover the contract:

1. Each intent routes to the *right* backend and to *only* that backend.
2. Concrete return shapes for the high-leverage paths (``is_thing_banned``
   for "provider-x", ``looking_up_decision`` for "billing_rail").
3. Unknown intents return ``recommendation="verify"`` with an explanatory
   ``finding``.
4. Defensive: missing index module / missing DB / missing table never crash.
5. ``cwd_hint=""`` widens to all projects; ``None`` defaults to current.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

import pytest

from mcp_server.extras import recall_targeted_tools as rtt

# ---------------------------------------------------------------------------
# Fixtures + utilities
# ---------------------------------------------------------------------------


class _NoCloseProxy:
    """Tool calls ``conn.close()`` in a ``finally``; tests share one conn."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, *a, **kw):
        return self._real.execute(*a, **kw)

    def executescript(self, *a, **kw):
        return self._real.executescript(*a, **kw)

    def close(self) -> None:
        pass


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def stub_conn(monkeypatch, conn):
    """Make ``recall_targeted`` see our in-memory conn via the proxy."""
    monkeypatch.setattr(rtt, "get_conn", lambda: _NoCloseProxy(conn))
    return conn


# ---------------------------------------------------------------------------
# Routing harness — each backend module is replaced with a recorder that
# returns a canned shape. We then assert that exactly one recorder fires.
# ---------------------------------------------------------------------------


class _CallRecorder:
    """Tracks how many times each backend was hit, plus the last args."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.last_args: dict[str, tuple] = {}

    def record(self, name: str, *args, **kw) -> None:
        self.calls[name] += 1
        self.last_args[name] = (args, kw)


def _install_recording_backends(monkeypatch) -> _CallRecorder:
    """Replace every ``_load(<modname>)`` target with a per-backend recorder.

    Returns the recorder so the test can assert which backend was hit. Every
    backend returns the *minimum* shape its route needs to take the "happy
    path" through to a non-empty result.
    """
    rec = _CallRecorder()

    class _BansStub:
        @staticmethod
        def check_banned(conn, thing):
            rec.record("bans.check_banned", thing)
            return {
                "banned_thing": thing,
                "ban_strength": "absolute",
                "ban_text": f"never use {thing}",
                "context_clause": None,
                "reassertion_count": 1,
            }

    class _DecisionsStub:
        @staticmethod
        def get_for_topic(conn, *, topic, scope=None):
            rec.record("decisions.get_for_topic", topic, scope)
            return {
                "topic": topic,
                "chose": "PayPal",
                "over": "Stripe",
                "rationale": "stripe is banned",
                "assertion_count": 3,
                "is_reversed": 0,
            }

    class _QueryStub:
        @staticmethod
        def search_extractions(conn, query=None, cwd=None, kind=None, limit=10, **kw):
            rec.record(
                "query.search_extractions",
                query,
                cwd,
                kind,
                limit,
            )

            # Return a single hit shaped like a QueryHit-ish dict.
            class _H:
                def __init__(self) -> None:
                    self.kind = kind or "model_correction"
                    self.content = f"stop suggesting {query}"
                    self.session_id = "s-1"
                    self.cwd = cwd or "/x"
                    self.score = 0.82

            return [_H()]

        @staticmethod
        def search_messages(conn, query=None, cwd=None, limit=10, **kw):
            rec.record("query.search_messages", query, cwd, limit)
            return [{"text": f"talking about {query}"}]

    class _GoalsStub:
        @staticmethod
        def get_active_goal(conn, project):
            rec.record("goals.get_active_goal", project)

            class _G:
                def to_dict(self) -> dict:
                    return {
                        "goal_text": "ship v0.3 MCP tools",
                        "status": "active",
                        "project": project,
                    }

            return _G()

    class _OperatorStub:
        @staticmethod
        def get_profile_field(conn, key):
            rec.record("operator.get_profile_field", key)
            return ("PayPal", 0.9, ["sess-1:42"])

    # Route ``_load`` through the stubs by name.
    table = {
        "bans": _BansStub,
        "decisions": _DecisionsStub,
        "query": _QueryStub,
        "goals": _GoalsStub,
        "operator": _OperatorStub,
    }
    monkeypatch.setattr(rtt, "_load", lambda name: table.get(name))
    return rec


# ---------------------------------------------------------------------------
# 1. Each intent routes to *only* the right backend
# ---------------------------------------------------------------------------


def test_intent_is_thing_banned_only_calls_bans(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("is_thing_banned", "provider-x")
    assert out["recommendation"] == "avoid"
    # Bans called exactly once; nothing else fired.
    assert rec.calls["bans.check_banned"] == 1
    assert sum(rec.calls.values()) == 1, rec.calls


def test_intent_looking_up_decision_only_calls_decisions(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("looking_up_decision", "billing_rail")
    assert out["recommendation"] == "use"
    assert rec.calls["decisions.get_for_topic"] == 1
    assert sum(rec.calls.values()) == 1, rec.calls


def test_intent_checking_past_correction_only_calls_query(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("checking_past_correction", "provider-x")
    assert out["recommendation"] == "avoid"
    # search_extractions called exactly once, filtered to model_correction.
    assert rec.calls["query.search_extractions"] == 1
    args, kw = rec.last_args["query.search_extractions"]
    # signature: (query, cwd, kind, limit)
    assert args[0] == "provider-x"
    assert args[2] == "model_correction"
    # No other backend fired.
    assert sum(rec.calls.values()) == 1


def test_intent_what_is_active_goal_only_calls_goals(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("what_is_active_goal", "")
    assert out["recommendation"] == "use"
    assert "ship v0.3 MCP tools" in out["finding"]
    assert rec.calls["goals.get_active_goal"] == 1
    assert sum(rec.calls.values()) == 1


def test_intent_have_we_discussed_uses_extractions_first(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("have_we_discussed_this", "wireguard")
    assert out["recommendation"] == "verify"  # discussion ≠ alignment
    assert rec.calls["query.search_extractions"] == 1
    # Extractions returned a hit, so messages fallback should NOT fire.
    assert rec.calls["query.search_messages"] == 0


def test_intent_operator_preference_lookup_hits_bans_and_operator(
    monkeypatch, stub_conn
):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("operator_preference_lookup", "cloud_provider")
    # Ban stub fires (strong negative) → recommendation collapses to "avoid".
    assert out["recommendation"] == "avoid"
    assert rec.calls["bans.check_banned"] == 1
    assert rec.calls["operator.get_profile_field"] == 1
    # No goals / decisions / FTS in this route.
    assert rec.calls["goals.get_active_goal"] == 0
    assert rec.calls["query.search_extractions"] == 0


def test_intent_before_suggesting_default_short_circuits_on_ban(
    monkeypatch, stub_conn
):
    """The composite route checks bans → decisions → corrections in order.

    A positive ban hit should short-circuit and *not* call decisions /
    corrections, mirroring "strongest negative signal wins" priority.
    """
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("before_suggesting_default", "provider-x")
    assert out["recommendation"] == "avoid"
    assert out["finding"].startswith("BAN:")
    assert rec.calls["bans.check_banned"] == 1
    # Decisions + corrections must NOT have fired.
    assert rec.calls["decisions.get_for_topic"] == 0
    assert rec.calls["query.search_extractions"] == 0


# ---------------------------------------------------------------------------
# 2. Concrete-shape tests against real index modules (in-memory DB)
# ---------------------------------------------------------------------------


def test_is_thing_banned_returns_avoid_with_verbatim_quote(monkeypatch, conn):
    """``check_banned`` on "provider-x" → avoid + verbatim quote, against the
    real ``index.bans`` schema. No backend mocking — full round trip."""
    pytest.importorskip("index.bans")
    from index.bans import ensure_schema, upsert_ban

    ensure_schema(conn)
    upsert_ban(
        conn,
        banned_thing="provider-x",
        ban_strength="absolute",
        ban_text="never use provider-x — they nuked our box",
    )

    monkeypatch.setattr(rtt, "get_conn", lambda: _NoCloseProxy(conn))
    out = rtt.recall_targeted("is_thing_banned", "provider-x")

    assert out["recommendation"] == "avoid"
    assert "provider-x" in out["finding"].lower()
    assert out["verbatim_quotes"], "must surface the operator's verbatim ban quote"
    assert "provider-x" in out["verbatim_quotes"][0].lower()
    assert out["confidence"] >= 0.9  # absolute strength → ≥0.95 floor


def test_looking_up_decision_billing_rail_returns_paypal(monkeypatch, conn):
    """``get_for_topic('billing_rail')`` returns PayPal with confidence > 0.7."""
    pytest.importorskip("index.decisions")
    from index.decisions import ensure_schema, upsert_decision

    ensure_schema(conn)
    upsert_decision(
        conn,
        topic="billing_rail",
        chose="PayPal",
        over="Stripe",
        rationale="stripe is banned for acme-net",
        scope="global",
    )

    monkeypatch.setattr(rtt, "get_conn", lambda: _NoCloseProxy(conn))
    out = rtt.recall_targeted("looking_up_decision", "billing_rail")

    assert out["recommendation"] == "use"
    assert "paypal" in out["finding"].lower()
    assert out["confidence"] > 0.7
    # Rationale should be surfaced as a verbatim quote.
    assert any("stripe" in q.lower() for q in out["verbatim_quotes"])


# ---------------------------------------------------------------------------
# 3. Unknown intent → verify with explanatory finding
# ---------------------------------------------------------------------------


def test_unknown_intent_returns_verify_with_explanatory_finding(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    # Bypass Literal typing — at runtime the SDK can pass through anything.
    out = rtt.recall_targeted("not_a_real_intent", "provider-x")  # type: ignore[arg-type]
    assert out["recommendation"] == "verify"
    assert "unknown intent" in out["finding"].lower()
    # No backend should have been called.
    assert sum(rec.calls.values()) == 0


# ---------------------------------------------------------------------------
# 4. Defensive: missing components never crash
# ---------------------------------------------------------------------------


def test_missing_db_returns_verify(monkeypatch):
    monkeypatch.setattr(rtt, "get_conn", lambda: None)
    out = rtt.recall_targeted("is_thing_banned", "provider-x")
    assert out["recommendation"] == "verify"
    assert "index not initialized" in out["finding"].lower()
    # The shape contract still holds.
    assert "verbatim_quotes" in out and out["verbatim_quotes"] == []
    assert "confidence" in out


def test_missing_index_module_returns_empty_not_crash(monkeypatch, stub_conn):
    """If ``index.bans`` isn't importable on this branch, we degrade."""
    monkeypatch.setattr(rtt, "_load", lambda name: None)
    out = rtt.recall_targeted("is_thing_banned", "provider-x")
    assert out["recommendation"] == "verify"
    assert "not available" in out["finding"].lower()


def test_missing_table_returns_empty_not_crash(monkeypatch, conn):
    """``standing_decisions`` table absent → ``OperationalError`` is swallowed."""

    def _raise_oprr(*a, **kw):
        raise sqlite3.OperationalError("no such table: standing_decisions")

    class _MissingTableDecisions:
        get_for_topic = staticmethod(_raise_oprr)

    monkeypatch.setattr(rtt, "get_conn", lambda: _NoCloseProxy(conn))
    monkeypatch.setattr(
        rtt,
        "_load",
        lambda name: _MissingTableDecisions if name == "decisions" else None,
    )
    out = rtt.recall_targeted("looking_up_decision", "billing_rail")
    assert out["recommendation"] == "verify"
    assert "no decision recorded" in out["finding"].lower()


def test_empty_subject_for_lookup_intent_returns_verify(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    out = rtt.recall_targeted("is_thing_banned", "   ")
    assert out["recommendation"] == "verify"
    # Crucially, no backend fired — we rejected at validation.
    assert sum(rec.calls.values()) == 0


def test_have_we_discussed_falls_back_to_messages_when_extractions_empty(
    monkeypatch, stub_conn
):
    """If extractions FTS returns nothing, the route should sweep messages."""
    rec = _CallRecorder()

    class _Q:
        @staticmethod
        def search_extractions(conn, query=None, cwd=None, limit=5, **kw):
            rec.record("query.search_extractions", query, cwd)
            return []

        @staticmethod
        def search_messages(conn, query=None, cwd=None, limit=5, **kw):
            rec.record("query.search_messages", query, cwd)
            return [{"text": "yes we talked about " + str(query)}]

    monkeypatch.setattr(rtt, "_load", lambda name: _Q if name == "query" else None)
    out = rtt.recall_targeted("have_we_discussed_this", "kubernetes")
    assert out["recommendation"] == "verify"
    assert out["finding"].startswith("1 prior mention")
    assert rec.calls["query.search_extractions"] == 1
    assert rec.calls["query.search_messages"] == 1


# ---------------------------------------------------------------------------
# 5. cwd_hint handling
# ---------------------------------------------------------------------------


def test_cwd_hint_empty_string_widens_to_all_projects(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    rtt.recall_targeted("checking_past_correction", "kafka", cwd_hint="")
    args, _ = rec.last_args["query.search_extractions"]
    # signature: (query, cwd, kind, limit) — cwd must be None (= all projects).
    assert args[1] is None


def test_cwd_hint_explicit_scopes_lookup(monkeypatch, stub_conn):
    rec = _install_recording_backends(monkeypatch)
    rtt.recall_targeted(
        "checking_past_correction", "kafka", cwd_hint="/home/operator/foo"
    )
    args, _ = rec.last_args["query.search_extractions"]
    assert args[1] == "/home/operator/foo"


# ---------------------------------------------------------------------------
# 6. Return shape is always the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intent,subject",
    [
        ("is_thing_banned", "redis"),
        ("looking_up_decision", "db_engine"),
        ("checking_past_correction", "nginx"),
        ("operator_preference_lookup", "editor"),
        ("have_we_discussed_this", "rate_limiting"),
        ("what_is_active_goal", ""),
        ("before_suggesting_default", "rabbitmq"),
    ],
)
def test_return_shape_invariant(monkeypatch, stub_conn, intent, subject):
    _install_recording_backends(monkeypatch)
    out: Any = rtt.recall_targeted(intent, subject)  # type: ignore[arg-type]
    assert isinstance(out, dict)
    assert "finding" in out and isinstance(out["finding"], str)
    assert "confidence" in out and 0.0 <= float(out["confidence"]) <= 1.0
    assert "verbatim_quotes" in out and isinstance(out["verbatim_quotes"], list)
    assert out["recommendation"] in ("use", "avoid", "verify")
