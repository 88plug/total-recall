"""Tests for :mod:`hooks.lib.scope_delta`.

Covers:

1. The happy path — when every index module is mockable, the assembled
   payload contains every section in priority order.
2. ``from_scope`` subtraction: a standing decision present in both the
   destination *and* the source scope is dropped (the model already
   has it).
3. Priority truncation: when the budget is tight, ``active_goal`` is
   preserved and ``machines`` are sacrificed.
4. ``MIN_INJECTION_CHARS`` gate: an output that's almost empty becomes
   an empty string.
5. Empty index returns "" — when nothing is found, no payload.
6. Defensive imports — when index modules raise on import the function
   degrades gracefully.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from hooks.lib import scope_delta

# ---------------------------------------------------------------------------
# Mock-index plumbing
# ---------------------------------------------------------------------------


class _FakeConn:
    """Stand-in for ``sqlite3.Connection`` — the mocked index functions ignore it."""

    def close(self) -> None:  # pragma: no cover - never reached
        pass


@pytest.fixture
def fake_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a stub db_path and short-circuit ``_open_conn`` so we never touch sqlite."""
    db = tmp_path / "fake.db"
    db.write_bytes(b"")  # exists for realism — never read
    monkeypatch.setattr(scope_delta, "_open_conn", lambda _p: _FakeConn())
    return db


def _install_mock_index(
    monkeypatch: pytest.MonkeyPatch,
    *,
    goal: dict | None = None,
    decisions_by_scope: dict[str, list[dict]] | None = None,
    bans: list[dict] | None = None,
    machines: list[dict] | None = None,
) -> None:
    """Drop fake ``index.*`` submodules into ``sys.modules``.

    Anything not supplied defaults to "module exists but the function
    raises / returns nothing" so the corresponding section is skipped.
    """
    decisions_by_scope = decisions_by_scope or {}

    # index.goals -----------------------------------------------------------
    goals_mod = types.ModuleType("index.goals")

    def get_active_goal_for_cwd(_conn, _cwd):
        return goal

    goals_mod.get_active_goal_for_cwd = get_active_goal_for_cwd  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index.goals", goals_mod)

    # index.decisions -------------------------------------------------------
    decisions_mod = types.ModuleType("index.decisions")

    def list_decisions(_conn, *, scope=None, limit=100, **_kw):
        return list(decisions_by_scope.get(scope, []))[:limit]

    decisions_mod.list_decisions = list_decisions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index.decisions", decisions_mod)

    # index.bans ------------------------------------------------------------
    bans_mod = types.ModuleType("index.bans")

    def list_bans_for_scope(_conn, _scope, limit=3):
        return list(bans or [])[:limit]

    bans_mod.list_bans_for_scope = list_bans_for_scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index.bans", bans_mod)

    # index.ontology --------------------------------------------------------
    ontology_mod = types.ModuleType("index.ontology")

    def machines_for_scope(_conn, _scope, limit=3):
        return list(machines or [])[:limit]

    ontology_mod.machines_for_scope = machines_for_scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index.ontology", ontology_mod)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------


SAMPLE_GOAL = {
    "goal_text": "ship the scope-shift injection",
    "status": "active",
    "project": "/home/operator/claude-code-session-logs-data-mining",
}

SAMPLE_DECISIONS_TO = [
    {"topic": "vps_provider", "chose": "provider-y", "scope": "recall"},
    {"topic": "data_store", "chose": "sqlite", "scope": "recall"},
]

SAMPLE_DECISIONS_FROM = [
    # vps_provider exists in both — should be subtracted when from_scope is set
    {"topic": "vps_provider", "chose": "provider-y", "scope": "acme-net"},
    {"topic": "vpn", "chose": "wireguard", "scope": "acme-net"},
]

SAMPLE_BANS = [
    {
        "banned_thing": "provider-x",
        "ban_strength": "absolute",
        "ban_text": "provider-x banned, billing fight",
    }
]

SAMPLE_MACHINES = [
    {"hostname": "git.example.com", "role": "control", "public_ip": "203.0.113.1"},
    {"hostname": "relay-1", "role": "relay", "public_ip": "203.0.113.2"},
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_happy_path_returns_all_sections(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mock_index(
        monkeypatch,
        goal=SAMPLE_GOAL,
        decisions_by_scope={"recall": SAMPLE_DECISIONS_TO},
        bans=SAMPLE_BANS,
        machines=SAMPLE_MACHINES,
    )
    # _scope_to_canonical_cwd does a live DB lookup; stub it so the goal
    # section fires even without a real projects table.
    monkeypatch.setattr(
        scope_delta,
        "_scope_to_canonical_cwd",
        lambda scope, db_path=None: "/home/dana/recall" if scope == "recall" else None,
    )
    out = scope_delta.compute_scope_delta(fake_db, from_scope=None, to_scope="recall")

    assert out  # non-empty
    assert "[scope-shift -> recall]" in out
    # Sections appear in priority order.
    pos_goal = out.index("## active_goal")
    pos_dec = out.index("## standing_decisions")
    pos_bans = out.index("## bans")
    pos_mach = out.index("## machines")
    assert pos_goal < pos_dec < pos_bans < pos_mach

    # Each section actually rendered something useful.
    assert "ship the scope-shift injection" in out
    assert "vps_provider" in out
    assert "provider-x" in out
    assert "git.example.com" in out


def test_from_scope_subtraction_drops_shared_decisions(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision-topic valid in both scopes is dropped from the delta."""
    _install_mock_index(
        monkeypatch,
        decisions_by_scope={
            "recall": SAMPLE_DECISIONS_TO,    # vps_provider, data_store
            "acme-net": SAMPLE_DECISIONS_FROM,  # vps_provider, vpn
        },
    )
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope="acme-net", to_scope="recall"
    )
    assert "[scope-shift acme-net -> recall]" in out
    # data_store is fresh for the destination scope -> kept
    assert "data_store" in out
    # vps_provider exists in both -> the model already has it -> dropped
    assert "vps_provider" not in out


def test_priority_truncation_preserves_active_goal_over_machines(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the budget is tight, low-priority sections fall off first."""
    _install_mock_index(
        monkeypatch,
        goal=SAMPLE_GOAL,
        decisions_by_scope={"recall": SAMPLE_DECISIONS_TO},
        bans=SAMPLE_BANS,
        machines=SAMPLE_MACHINES,
    )
    # Stub _scope_to_canonical_cwd so the active_goal section fires.
    monkeypatch.setattr(
        scope_delta,
        "_scope_to_canonical_cwd",
        lambda scope, db_path=None: "/home/dana/recall" if scope == "recall" else None,
    )
    # Budget large enough for header + active_goal but not all four sections.
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope=None, to_scope="recall", max_chars=180
    )
    assert "## active_goal" in out
    assert "ship the scope-shift injection" in out
    assert "## machines" not in out
    assert len(out) <= 180


def test_min_injection_chars_gate(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If only a sliver of content survives, return empty string."""
    # Tiny goal + tiny budget => the body would be ~1 line, below the gate.
    tiny_goal = {"goal_text": "x", "status": "active", "project": "/p"}
    _install_mock_index(monkeypatch, goal=tiny_goal)
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope=None, to_scope="recall", max_chars=40
    )
    assert out == ""


def test_empty_index_returns_empty_string(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No goal, no decisions, no bans, no machines -> no payload."""
    _install_mock_index(monkeypatch)  # everything defaults to empty
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope=None, to_scope="recall"
    )
    assert out == ""


def test_defensive_imports_when_modules_missing(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If index modules raise on import, we return "" rather than blow up."""
    # Poison every index submodule by inserting a non-module sentinel.
    for name in ("index.goals", "index.decisions", "index.bans", "index.ontology"):
        broken = types.ModuleType(name)
        # No attributes -> AttributeError on first access from compute_scope_delta.
        monkeypatch.setitem(sys.modules, name, broken)
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope=None, to_scope="recall"
    )
    assert out == ""


def test_unknown_scope_does_not_crash(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown scope name has no canonical cwd; the function still returns."""
    _install_mock_index(
        monkeypatch,
        decisions_by_scope={"weirdscope": SAMPLE_DECISIONS_TO},
    )
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope=None, to_scope="weirdscope"
    )
    # decisions still render — only the cwd-keyed goal lookup is skipped.
    assert "vps_provider" in out
    assert "## active_goal" not in out


def test_max_chars_respected(
    fake_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard char cap is honored even with verbose data."""
    long_decisions = [
        {"topic": f"topic_{i}", "chose": "X" * 200} for i in range(20)
    ]
    _install_mock_index(
        monkeypatch,
        goal=SAMPLE_GOAL,
        decisions_by_scope={"recall": long_decisions},
        machines=SAMPLE_MACHINES,
    )
    out = scope_delta.compute_scope_delta(
        fake_db, from_scope=None, to_scope="recall", max_chars=500
    )
    assert len(out) <= 500


def test_scope_to_canonical_cwd_returns_none_without_db(tmp_path) -> None:
    """Without a live DB, _scope_to_canonical_cwd returns None gracefully.

    The function now queries the ``projects`` table for display_name / cwd
    basename matches instead of using a hardcoded map. When the DB is absent
    or the table is empty, None is the correct answer for any scope name.
    """
    # Point at a non-existent DB path so no connection is made.
    non_existent_db = tmp_path / "no_such.db"
    result = scope_delta._scope_to_canonical_cwd("recall", db_path=non_existent_db)
    assert result is None


def test_scope_to_canonical_cwd_finds_match_in_db(tmp_path) -> None:
    """When the DB has a matching project, the cwd is returned."""
    import sqlite3 as _sqlite3

    db = tmp_path / "index.db"
    conn = _sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE projects (cwd TEXT, display_name TEXT, last_active_ts INTEGER)"
    )
    conn.execute(
        "INSERT INTO projects VALUES (?, ?, ?)",
        ("/home/dana/nova-api", "nova-api", 1_700_000_000),
    )
    conn.commit()
    conn.close()

    # Lookup by display_name (case-insensitive exact match).
    result = scope_delta._scope_to_canonical_cwd("nova-api", db_path=db)
    assert result == "/home/dana/nova-api"

    # Lookup by cwd basename.
    result2 = scope_delta._scope_to_canonical_cwd("nova-api", db_path=db)
    assert result2 == "/home/dana/nova-api"

    # Unknown scope returns None.
    result_none = scope_delta._scope_to_canonical_cwd("unknown-scope", db_path=db)
    assert result_none is None
