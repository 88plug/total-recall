"""Tests for the standing-decisions extractor + store.

Covers:

1. Each O5 pattern fires correctly (chose, instead_of, door, ban,
   ban_we, migrated_away, abandoned, money_burn, wins_because).
2. Topic normalization maps known tokens to the canonical vocabulary
   and falls back to ``misc`` for everything else.
3. The :func:`upsert_decision` query is idempotent on
   ``(topic, chose, scope)`` and bumps ``assertion_count`` /
   ``last_reasserted_ts`` on every re-statement.
4. :func:`mark_reversed` flips ``is_reversed`` and accumulates
   ``money_burn_usd``.
5. Money-burn parsing in the extractor surfaces a float on the
   extraction context.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from extractors.standing_decisions import (
    StandingDecisions,
    normalize_topic,
)
from index.decisions import (
    ensure_schema,
    get_for_topic,
    list_decisions,
    mark_reversed,
    upsert_decision,
)

# ---------------------------------------------------------------------------
# FakeRecord — match the duck-typed RecordLike protocol the extractor uses.
# ---------------------------------------------------------------------------


@dataclass
class FakeRecord:
    type: str
    uuid: str
    parent_uuid: str | None = None
    session_id: str = "sess-1"
    cwd: str = "/home/operator/proj"
    ts: datetime = field(
        default_factory=lambda: datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    )
    role: str | None = None
    content_kind: str | None = None
    content: Any = None
    text: str | None = None
    is_meta: bool = False
    is_compact_summary: bool = False
    is_sidechain: bool = False
    subtype: str | None = None
    payload: dict | None = None


def _user(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="user",
        uuid=kw.pop("uuid", f"u-{abs(hash(text)) % 10_000}"),
        role="user",
        content_kind="string",
        text=text,
        content=text,
        **kw,
    )


def _assistant(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="assistant",
        uuid=kw.pop("uuid", f"a-{abs(hash(text)) % 10_000}"),
        role="assistant",
        content_kind="blocks",
        text=text,
        content=[{"type": "text", "text": text}],
        **kw,
    )


def _extract(*records: FakeRecord) -> list:
    return list(StandingDecisions().extract(list(records)))


# ---------------------------------------------------------------------------
# 1) Each pattern fires correctly
# ---------------------------------------------------------------------------


def test_chose_pattern_fires_and_normalizes_topic():
    """`chose X` / `going with X` / `switching to X` -> standing_decision."""
    cases = [
        "we chose aws for the relay fleet",
        "going with stripe for billing",
        "sticking with WireGuard for the tunnel",
        "switching to gitlab for the forge",
        "moved to podman for the orchestrator",
    ]
    topics_seen = set()
    for text in cases:
        results = _extract(_user(text))
        assert results, f"expected a hit for {text!r}"
        ext = next(e for e in results if e.context.get("pattern") == "chose")
        assert ext.kind == "standing_decision"
        topics_seen.add(ext.context["topic"])
    # All five known topic families should be represented.
    assert topics_seen >= {
        "cloud_provider",
        "billing_rail",
        "tunnel",
        "forge",
        "container_orchestrator",
    }


def test_instead_of_pattern_captures_both_sides():
    """`X instead of Y` / `X rather than Y` / `X over Y`."""
    ext = _extract(_assistant("we picked aws instead of gcp because cost savings"))
    hit = next(e for e in ext if e.context.get("pattern") == "instead_of")
    assert hit.context["chose"].lower() == "aws"
    assert hit.context["over"].lower() == "gcp"
    assert hit.context["topic"] == "cloud_provider"

    ext = _extract(_assistant("Stripe rather than Paddle — Paddle blocks privacy biz"))
    hit = next(e for e in ext if e.context.get("pattern") == "instead_of")
    assert hit.context["chose"].lower() == "stripe"
    assert hit.context["over"].lower() == "paddle"
    assert hit.context["topic"] == "billing_rail"


def test_door_pattern_fires():
    ext = _extract(_user("let's do Door #3 on the cloud provider question"))
    assert any(e.context.get("pattern") == "door" for e in ext)


def test_ban_patterns_fire():
    """`never recommend X` and `we never use X`."""
    bans = _extract(
        _user("never recommend aws for gcp-friendly setups"),
        _user("we never use paddle — stripe only"),
        _user("we don't use openvpn anymore"),
    )
    patterns = {e.context["pattern"] for e in bans}
    assert "ban" in patterns
    assert "ban_we" in patterns
    # The banned token comes through as `over` and `chose` is prefixed BAN:
    chose_vals = [e.context["chose"] for e in bans if e.context["pattern"].startswith("ban")]
    assert any(v.startswith("BAN:") for v in chose_vals)


def test_reversal_patterns_set_is_reversed():
    """`MIGRATED AWAY FROM X` and `abandoned X`."""
    ext = _extract(_assistant("MIGRATED AWAY FROM aws after the $200 burn"))
    rev = [e for e in ext if e.context.get("pattern") == "migrated_away"]
    assert rev and rev[0].context.get("is_reversed") is True
    assert rev[0].context.get("reversed_to", "").lower() == "aws"

    ext = _extract(_assistant("abandoned stripe last quarter"))
    assert any(
        e.context.get("pattern") == "abandoned" and e.context.get("is_reversed")
        for e in ext
    )


def test_money_burn_pattern_parses_amount():
    ext = _extract(
        _user("that's $200 burned learning aws doesn't do privacy"),
    )
    money = [e for e in ext if e.context.get("pattern") == "money_burn"]
    assert money, "expected money_burn extraction"
    assert money[0].context.get("money_burn_usd") == pytest.approx(200.0)


def test_wins_because_pattern():
    ext = _extract(
        _assistant("Vultr wins because they don't ToS-ban privacy infra")
    )
    hit = [e for e in ext if e.context.get("pattern") == "wins_because"]
    assert hit
    assert hit[0].context["chose"].lower() == "vultr"
    assert "rationale" in hit[0].context
    assert "ToS" in hit[0].context["rationale"] or "tos" in hit[0].context["rationale"].lower()


# ---------------------------------------------------------------------------
# 2) Topic normalization
# ---------------------------------------------------------------------------


def test_topic_normalization_maps_known_tokens_and_falls_back_to_misc():
    assert normalize_topic("aws") == "cloud_provider"
    assert normalize_topic("gcp") == "cloud_provider"
    assert normalize_topic("PayPal") == "billing_rail"
    assert normalize_topic("WireGuard") == "tunnel"
    assert normalize_topic("github") == "forge"
    assert normalize_topic("gitea") == "forge"
    assert normalize_topic("k8s") == "container_orchestrator"
    assert normalize_topic("podman") == "container_orchestrator"
    # Private hostname is no longer a canonical token — falls back to misc.
    assert normalize_topic("git.example.com") == "misc"
    # Unknown
    assert normalize_topic("squirrels") == "misc"
    assert normalize_topic(None) == "misc"
    assert normalize_topic("") == "misc"


# ---------------------------------------------------------------------------
# 3) UPSERT idempotency + assertion_count bump
# ---------------------------------------------------------------------------


def test_upsert_decision_increments_assertion_count_on_restatement():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    rid_a = upsert_decision(
        conn,
        topic="cloud_provider",
        chose="aws",
        scope="global",
        over="gcp",
        rationale="cost savings + no ToS hostility",
        ts=1_700_000_000,
        source_session="sess-1",
    )
    rid_b = upsert_decision(
        conn,
        topic="cloud_provider",
        chose="aws",
        scope="global",
        ts=1_700_000_500,
        source_session="sess-2",
    )
    assert rid_a == rid_b, "same UNIQUE key → same row id"

    row = conn.execute(
        "SELECT * FROM standing_decisions WHERE id=?", (rid_a,)
    ).fetchone()
    assert row["assertion_count"] == 2
    assert row["first_asserted_ts"] == 1_700_000_000
    assert row["last_reasserted_ts"] == 1_700_000_500
    # Rationale should *not* be overwritten by a second assertion w/o one.
    assert row["rationale"] == "cost savings + no ToS hostility"
    # First source_session wins (we COALESCE existing).
    assert row["source_session"] == "sess-1"


# ---------------------------------------------------------------------------
# 4) Reversal updates is_reversed + accumulates money_burn
# ---------------------------------------------------------------------------


def test_mark_reversed_flips_flag_and_accumulates_money_burn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    # Initial decision with a $50 burn already attached.
    upsert_decision(
        conn,
        topic="cloud_provider",
        chose="gcp",
        scope="global",
        money_burn_usd=50.0,
        ts=1_700_000_000,
    )
    ok = mark_reversed(
        conn,
        topic="cloud_provider",
        chose="gcp",
        scope="global",
        reversed_to="aws",
        money_burn_usd=150.0,
        ts=1_700_001_000,
    )
    assert ok is True

    row = conn.execute(
        "SELECT * FROM standing_decisions WHERE topic=? AND chose=? AND scope=?",
        ("cloud_provider", "gcp", "global"),
    ).fetchone()
    assert row["is_reversed"] == 1
    assert row["reversed_at_ts"] == 1_700_001_000
    assert row["reversed_to"] == "aws"
    assert row["money_burn_usd"] == pytest.approx(200.0)  # 50 + 150

    # mark_reversed on a non-existent row returns False and does not insert.
    miss = mark_reversed(
        conn, topic="cloud_provider", chose="nonexistent", scope="global"
    )
    assert miss is False


# ---------------------------------------------------------------------------
# 5) Read-path helpers
# ---------------------------------------------------------------------------


def test_list_and_get_for_topic_prefer_scope_then_global():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    upsert_decision(
        conn,
        topic="billing_rail",
        chose="stripe",
        scope="global",
        ts=1_700_000_000,
    )
    upsert_decision(
        conn,
        topic="billing_rail",
        chose="lemonsqueezy",
        scope="/home/operator/oneoff-project",
        ts=1_700_000_500,
    )
    # 3 re-assertions of stripe → assertion_count=4
    for _ in range(3):
        upsert_decision(
            conn,
            topic="billing_rail",
            chose="stripe",
            scope="global",
            ts=1_700_000_100,
        )

    # list_decisions: stripe first (higher assertion_count).
    rows = list_decisions(conn, topic="billing_rail")
    assert rows[0]["chose"] == "stripe"
    assert rows[0]["assertion_count"] == 4

    # get_for_topic with project scope: finds the project row exactly.
    proj = get_for_topic(
        conn, topic="billing_rail", scope="/home/operator/oneoff-project"
    )
    assert proj is not None and proj["chose"] == "lemonsqueezy"

    # get_for_topic with a different scope: falls back to the global row.
    other = get_for_topic(conn, topic="billing_rail", scope="/some/other/cwd")
    assert other is not None and other["chose"] == "stripe"
    assert other["scope"] == "global"
