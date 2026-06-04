"""FIX 2: the operator-context collectors call real production functions.

The four functions ``get_active_goal_for_cwd`` / ``top_decisions_for_scope`` /
``top_bans`` / ``machines_for_cwd`` previously did not exist, so the
corresponding sections silently vanished from ``get_operator_context``. These
tests seed a *real* synthetic DB (no module stubbing) and assert each section
materializes when data exists.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit tests for the four newly-implemented functions.
# ---------------------------------------------------------------------------


def _seed(conn: sqlite3.Connection) -> None:
    from index import goals as goals_idx
    from index import decisions as dec
    from index import bans as bans_idx
    from index import ontology as onto

    goals_idx.apply_schema(conn)
    dec.ensure_schema(conn)
    bans_idx.ensure_schema(conn)
    onto.ensure_schema(conn)


def test_get_active_goal_for_cwd_pools_worktree():
    from index.goals import get_active_goal_for_cwd, upsert_from_extractions
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed(conn)
    wt = "/home/operator/proj/.claude/worktrees/wf_a-1"
    parent = "/home/operator/proj"
    ext = type("E", (), {
        "kind": "goal", "content": "finish the migration",
        "ts": datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
        "cwd": wt, "session_id": "s1", "source_uuid": "u1",
        "score": 0.8, "scope": "project", "context": {},
    })()
    upsert_from_extractions(conn, [ext])

    # Pools: query by either worktree or parent cwd hits the same goal.
    g_parent = get_active_goal_for_cwd(conn, parent)
    g_wt = get_active_goal_for_cwd(conn, wt)
    assert g_parent is not None and g_parent.goal_text == "finish the migration"
    assert g_wt is not None and g_wt.goal_text == "finish the migration"
    # None-safe.
    assert get_active_goal_for_cwd(conn, None) is None


def test_top_decisions_for_scope_cascade():
    from index.decisions import top_decisions_for_scope, upsert_decision

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed(conn)
    proj = "/home/operator/proj"
    # A project-scoped decision and a global one.
    upsert_decision(conn, topic="db", chose="sqlite", over="postgres",
                    rationale="simple", scope=proj)
    upsert_decision(conn, topic="ci", chose="gha", over="jenkins",
                    rationale="hosted", scope="global")

    # Project scope (worktree collapses to proj) prefers the project row.
    wt = proj + "/.claude/worktrees/wf_x-1"
    rows = top_decisions_for_scope(conn, cwd=wt, limit=3)
    assert rows and any(r["topic"] == "db" for r in rows)

    # Unknown project with no project rows falls back to global.
    rows_unknown = top_decisions_for_scope(conn, cwd="/no/such/proj", limit=3)
    assert rows_unknown and all(r["scope"] == "global" for r in rows_unknown)

    # No cwd / empty table guard.
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    assert top_decisions_for_scope(empty, cwd=proj) == []


def test_top_bans_global_recency():
    from index.bans import top_bans, upsert_ban

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed(conn)
    upsert_ban(conn, banned_thing="provider-y", ban_strength="absolute",
               ban_text="never use provider-y", ts=100)
    upsert_ban(conn, banned_thing="jwt", ban_strength="preference",
               ban_text="prefer session tokens", ts=200)

    rows = top_bans(conn, cwd="/home/operator/proj", limit=3)
    assert rows
    # Absolute outranks preference.
    assert rows[0]["banned_thing"] == "provider-y"
    # cwd is accepted but ignored (symmetry with siblings).
    rows2 = top_bans(conn, limit=3)
    assert {r["banned_thing"] for r in rows2} == {"provider-y", "jwt"}
    # Empty-table guard.
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    assert top_bans(empty) == []


def test_machines_for_cwd_recency():
    from index.ontology import machines_for_cwd, upsert_machine

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed(conn)
    upsert_machine(conn, "old-box", role="legacy", last_seen_ts=100)
    upsert_machine(conn, "new-box", role="primary", last_seen_ts=999)

    rows = machines_for_cwd(conn, cwd="/home/operator/proj", limit=3)
    assert rows
    assert rows[0]["hostname"] == "new-box"  # newest last_seen_ts first
    # Empty-table guard.
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    assert machines_for_cwd(empty) == []


# ---------------------------------------------------------------------------
# Integration: get_operator_context against a real (un-stubbed) DB.
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def real_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(tmp_path))
    for mod in (
        "mcp_server", "mcp_server.server", "mcp_server.tools",
        "mcp_server.resources", "mcp_server.extras",
        "mcp_server.extras.operator_context_tools",
    ):
        sys.modules.pop(mod, None)
    return tmp_path


def test_get_operator_context_real_sections_appear(real_db_dir, monkeypatch):
    from datetime import datetime, timezone
    from index import goals as goals_idx
    from index import decisions as dec
    from index import bans as bans_idx
    from index import ontology as onto
    from index import operator as op

    db_path = real_db_dir / "index.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    op.ensure_schema(conn)
    op.upsert_profile_field(conn, "name", "Andrew", confidence=0.95)

    goals_idx.apply_schema(conn)
    goal_ext = type("E", (), {
        "kind": "goal", "content": "ship the operator-context aggregator",
        "ts": datetime.fromtimestamp(1_700_000_000, tz=timezone.utc),
        "cwd": "/home/operator/proj", "session_id": "s1", "source_uuid": "g1",
        "score": 0.8, "scope": "project", "context": {},
    })()
    goals_idx.upsert_from_extractions(conn, [goal_ext])

    dec.ensure_schema(conn)
    dec.upsert_decision(conn, topic="db", chose="sqlite", over="postgres",
                        rationale="simple", scope="/home/operator/proj")

    bans_idx.ensure_schema(conn)
    bans_idx.upsert_ban(conn, banned_thing="provider-y", ban_strength="absolute",
                        ban_text="never use provider-y", ts=100)

    onto.ensure_schema(conn)
    onto.upsert_machine(conn, "wildnuc", role="primary", last_seen_ts=999)

    conn.commit()
    conn.close()

    importlib.import_module("mcp_server.server")
    tool_mod = importlib.import_module(
        "mcp_server.extras.operator_context_tools"
    )

    out = tool_mod.get_operator_context(
        cwd="/home/operator/proj", max_chars=4000
    )
    assert isinstance(out, dict)
    # The four FIX-2 sections + identity must all materialize from real data.
    assert "identity" in out and out["identity"].get("name") == "Andrew"
    # active_goal must carry the real goal text (not a stringified dataclass).
    assert "active_goal" in out
    assert out["active_goal"].get("goal") == "ship the operator-context aggregator"
    # standing_decisions must carry real columns.
    assert "standing_decisions" in out and out["standing_decisions"]
    assert out["standing_decisions"][0].get("topic") == "db"
    # bans must carry collector-projected keys (target/reason), not empty dicts.
    assert "bans" in out and out["bans"]
    assert out["bans"][0].get("target") == "provider-y"
    # machines must render to a non-empty one-liner containing the hostname.
    assert "machines" in out and out["machines"]
    assert any("wildnuc" in m for m in out["machines"])
