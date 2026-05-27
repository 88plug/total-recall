"""Tests for the per-project goal stack (extractor + index + MCP tools).

The tests are organised in three groups:

1. **Extractor** — `extractors.goals.Goals` over synthetic `RecordLike`s
   covers the explicit-marker path, the first-message-after-permission-mode
   heuristic, and the `goal_progress` emission for assistant `Done.` /
   `Shipped` paragraphs.

2. **Index / state machine** — `index.goals` upsert + `recompute_statuses`
   driven by a fake clock fixture. Asserts the full 60-day timeline:
   active → paused (30d) → abandoned (60d), blocked → active on progress,
   `done` is terminal.

3. **MCP tools** — `get_active_goal` / `list_goals` against an in-memory
   DB. The MCP server module is imported with `TOTAL_RECALL_DB_DIR` set
   to a tmp path so we don't touch the operator's real index.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import time as _time_mod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from extractors.goals import Goals
from index import goals as goals_idx


# ---------------------------------------------------------------------------
# Shared fake-record helpers
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
        uuid=kw.pop("uuid", f"u-{abs(hash(text)) % 100_000}"),
        role="user",
        content_kind="string",
        text=text,
        content=text,
        **kw,
    )


def _assistant(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="assistant",
        uuid=kw.pop("uuid", f"a-{abs(hash(text)) % 100_000}"),
        role="assistant",
        content_kind="blocks",
        text=text,
        content=[{"type": "text", "text": text}],
        **kw,
    )


def _perm_mode(**kw: Any) -> FakeRecord:
    return FakeRecord(
        type="permission-mode",
        uuid=kw.pop("uuid", "p-1"),
        **kw,
    )


# ---------------------------------------------------------------------------
# Extractor: explicit-marker path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "goal: ship the goals MCP tool by friday",
        "trying to land the wireguard relay rotation today",
        "objective: cut p99 to under 80ms",
        "the point is to get host-alpha talking to relay-2 again",
        "what we want to do is migrate billing to stripe subscriptions",
        "let's ship the new admin panel",
        "let's launch v2 next week",
        "let's build the goal stack first",
        "let's figure out why CI is flaky",
        "lets ship the new admin panel",
        "need to ship the postcompact hook",
        "need to launch by EOQ",
        "need to build a real test rig",
    ],
)
def test_goals_explicit_markers_fire(text):
    rec = _user(text)
    results = [e for e in Goals().extract([rec]) if e.kind == "goal"]
    assert results, f"should match: {text!r}"
    ext = results[0]
    assert ext.context.get("marker") is True
    assert ext.score >= 0.65 + 0.15  # base + explicit-marker bonus


def test_goals_negative_cases_dont_fire_on_marker_alone():
    """A user-string with no goal marker AND not the first message → no goal."""
    # First message is consumed by the first-msg heuristic, so use the
    # second user turn to test "no marker, no fire".
    rec1 = _user("first turn here", uuid="u-1")
    rec2 = _user("the noop case", uuid="u-2")
    out = list(Goals().extract([rec1, rec2]))
    goal_kinds = [e for e in out if e.kind == "goal"]
    # Only the first message should have produced a goal.
    assert len(goal_kinds) == 1
    assert goal_kinds[0].source_uuid == "u-1"


def test_goals_meta_records_skipped():
    rec = _user("goal: do the thing")
    rec.is_meta = True
    assert not list(Goals().extract([rec]))


# ---------------------------------------------------------------------------
# Extractor: first-message-after-permission-mode heuristic
# ---------------------------------------------------------------------------


def test_first_user_after_permission_mode_is_a_goal():
    perm = _perm_mode()
    first_user = _user("get the goals MCP server wired up end-to-end", uuid="u-first")
    second_user = _user("now also add an FTS index", uuid="u-second")
    records = [perm, first_user, second_user]

    goal_exts = [e for e in Goals().extract(records) if e.kind == "goal"]
    # First user-string fires (first_message=True).
    first_hits = [e for e in goal_exts if e.source_uuid == "u-first"]
    assert len(first_hits) == 1
    assert first_hits[0].context["first_message"] is True
    # Second user-string is not a goal (no explicit marker, not first).
    second_hits = [e for e in goal_exts if e.source_uuid == "u-second"]
    assert not second_hits


def test_first_message_credit_only_once_per_session():
    """Two sessions, each gets a first-message goal — but not the second turn."""
    s1_u1 = _user("session 1 goal text here", uuid="s1-u1", session_id="sess-1")
    s1_u2 = _user("session 1 second turn", uuid="s1-u2", session_id="sess-1")
    s2_u1 = _user("session 2 goal text here", uuid="s2-u1", session_id="sess-2")
    records = [s1_u1, s1_u2, s2_u1]

    goal_exts = [e for e in Goals().extract(records) if e.kind == "goal"]
    uuids = {e.source_uuid for e in goal_exts}
    assert "s1-u1" in uuids
    assert "s2-u1" in uuids
    assert "s1-u2" not in uuids  # second turn of session 1: no credit


# ---------------------------------------------------------------------------
# Extractor: goal_progress emission from assistant Done./Shipped paragraphs
# ---------------------------------------------------------------------------


def test_assistant_done_emits_goal_progress():
    rec = _assistant("Done. wired up the FTS triggers and ran the index pass.")
    out = list(Goals().extract([rec]))
    progress = [e for e in out if e.kind == "goal_progress"]
    assert len(progress) == 1
    assert progress[0].content.startswith("Done.")


def test_assistant_shipped_committed_fixed_all_emit():
    for marker in ("Shipped the rotation script.", "Committed the migration.",
                   "Fixed the off-by-one in the cursor."):
        rec = _assistant(marker)
        progress = [e for e in Goals().extract([rec]) if e.kind == "goal_progress"]
        assert progress, f"{marker!r} should emit goal_progress"


def test_assistant_narrative_done_does_not_fire():
    """`Done.` inside a paragraph (not at paragraph start) is not a marker."""
    rec = _assistant("This is fine. Done. is fine too — but not as a marker mid-para.")
    progress = [e for e in Goals().extract([rec]) if e.kind == "goal_progress"]
    # `Done.` appears mid-paragraph so the anchored regex doesn't match.
    assert not progress


# ---------------------------------------------------------------------------
# Index schema + upsert
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    goals_idx.apply_schema(c)
    return c


def _ext(kind: str, content: str, ts: int, cwd: str = "/home/operator/proj",
         sid: str = "sess-1", context: dict | None = None):
    """Build a Plain Old Object that quacks like Extraction enough for upsert."""
    return type("E", (), {
        "kind": kind, "content": content,
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc),
        "cwd": cwd, "session_id": sid, "source_uuid": f"u-{ts}",
        "score": 0.8, "scope": "project",
        "context": context or {},
    })()


def test_upsert_inserts_goal(conn: sqlite3.Connection):
    ts = 1_700_000_000
    n_goals, n_prog = goals_idx.upsert_from_extractions(
        conn, [_ext("goal", "ship the goals tool", ts)]
    )
    assert n_goals == 1
    rows = list(conn.execute("SELECT * FROM goal_stack"))
    assert len(rows) == 1
    assert rows[0]["goal_text"] == "ship the goals tool"
    assert rows[0]["status"] == "active"
    assert rows[0]["declared_ts"] == ts


def test_upsert_idempotent_on_same_goal(conn: sqlite3.Connection):
    ts = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "X", ts)])
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "X", ts)])
    assert conn.execute("SELECT COUNT(*) FROM goal_stack").fetchone()[0] == 1


def test_upsert_progress_bumps_last_progress_ts(conn: sqlite3.Connection):
    declared = 1_700_000_000
    progress = declared + 5 * 86_400
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "X", declared)])
    goals_idx.upsert_from_extractions(
        conn, [_ext("goal_progress", "Done. things.", progress)]
    )
    row = conn.execute(
        "SELECT last_progress_ts FROM goal_stack WHERE goal_text='X'"
    ).fetchone()
    assert row["last_progress_ts"] == progress


def test_upsert_status_hint_done_marks_done(conn: sqlite3.Connection):
    ts = 1_700_000_000
    goals_idx.upsert_from_extractions(
        conn,
        [_ext("goal", "X done now", ts, context={"status_hint": "done"})],
    )
    row = conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='X done now'"
    ).fetchone()
    assert row["status"] == "done"


def test_upsert_status_hint_blocked_marks_blocked(conn: sqlite3.Connection):
    ts = 1_700_000_000
    goals_idx.upsert_from_extractions(
        conn,
        [_ext("goal", "X is stuck", ts, context={"status_hint": "blocked"})],
    )
    row = conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='X is stuck'"
    ).fetchone()
    assert row["status"] == "blocked"


# ---------------------------------------------------------------------------
# State machine: 60-day timeline with a clock fixture
# ---------------------------------------------------------------------------


def test_active_to_paused_after_30_days(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "G", t0)])

    # 29d: still active.
    counts = goals_idx.recompute_statuses(conn, now_ts=t0 + 29 * 86_400)
    assert counts["active_to_paused"] == 0
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G'"
    ).fetchone()[0] == "active"

    # 31d: paused.
    counts = goals_idx.recompute_statuses(conn, now_ts=t0 + 31 * 86_400)
    assert counts["active_to_paused"] == 1
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G'"
    ).fetchone()[0] == "paused"


def test_paused_to_abandoned_after_60_days(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "G", t0)])
    # Step 1: 31d → paused.
    goals_idx.recompute_statuses(conn, now_ts=t0 + 31 * 86_400)
    # Step 2: 61d from declared → abandoned.
    counts = goals_idx.recompute_statuses(conn, now_ts=t0 + 61 * 86_400)
    assert counts["paused_to_abandoned"] == 1
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G'"
    ).fetchone()[0] == "abandoned"


def test_active_jumps_straight_to_abandoned_after_60_days(conn: sqlite3.Connection):
    """If recompute hasn't run between days 30-60, the active goal should
    end up abandoned in one pass (not paused — abandoned check runs first)."""
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "G", t0)])
    counts = goals_idx.recompute_statuses(conn, now_ts=t0 + 61 * 86_400)
    assert counts["active_to_abandoned"] == 1
    assert counts["active_to_paused"] == 0
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G'"
    ).fetchone()[0] == "abandoned"


def test_done_is_terminal(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(
        conn, [_ext("goal", "G done", t0, context={"status_hint": "done"})]
    )
    # 90d later: should still be done, never abandoned.
    goals_idx.recompute_statuses(conn, now_ts=t0 + 90 * 86_400)
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G done'"
    ).fetchone()[0] == "done"


def test_blocked_to_active_on_fresh_progress(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(
        conn, [_ext("goal", "G", t0, context={"status_hint": "blocked"})]
    )
    # Progress 5 days later → still blocked until recompute runs.
    goals_idx.upsert_from_extractions(
        conn, [_ext("goal_progress", "Done.", t0 + 5 * 86_400)]
    )
    counts = goals_idx.recompute_statuses(conn, now_ts=t0 + 6 * 86_400)
    assert counts["blocked_to_active"] == 1
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G'"
    ).fetchone()[0] == "active"


def test_progress_resets_pause_clock(conn: sqlite3.Connection):
    """A goal declared at t0, progress at t0+45d → not paused at t0+50d."""
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [_ext("goal", "G", t0)])
    goals_idx.upsert_from_extractions(
        conn, [_ext("goal_progress", "Shipped.", t0 + 45 * 86_400)]
    )
    counts = goals_idx.recompute_statuses(conn, now_ts=t0 + 50 * 86_400)
    assert counts["active_to_paused"] == 0
    assert conn.execute(
        "SELECT status FROM goal_stack WHERE goal_text='G'"
    ).fetchone()[0] == "active"


# ---------------------------------------------------------------------------
# Read queries
# ---------------------------------------------------------------------------


def test_get_active_goal_returns_most_recent(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [
        _ext("goal", "older goal", t0),
        _ext("goal", "newer goal", t0 + 1000),
    ])
    g = goals_idx.get_active_goal(conn, "/home/operator/proj")
    assert g is not None
    assert g.goal_text == "newer goal"


def test_get_active_goal_skips_terminal_states(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [
        _ext("goal", "old done", t0 + 2000, context={"status_hint": "done"}),
        _ext("goal", "actively progressing", t0 + 1000),
    ])
    g = goals_idx.get_active_goal(conn, "/home/operator/proj")
    assert g is not None
    assert g.goal_text == "actively progressing"


def test_get_active_goal_returns_none_when_empty(conn: sqlite3.Connection):
    assert goals_idx.get_active_goal(conn, "/nope") is None


def test_list_goals_filters_by_status(conn: sqlite3.Connection):
    t0 = 1_700_000_000
    goals_idx.upsert_from_extractions(conn, [
        _ext("goal", "a", t0),
        _ext("goal", "b done", t0 + 100, context={"status_hint": "done"}),
        _ext("goal", "c blocked", t0 + 200, context={"status_hint": "blocked"}),
    ])
    actives = goals_idx.list_goals(conn, project="/home/operator/proj", status="active")
    assert [g.goal_text for g in actives] == ["a"]
    dones = goals_idx.list_goals(conn, project="/home/operator/proj", status="done")
    assert [g.goal_text for g in dones] == ["b done"]
    anys = goals_idx.list_goals(conn, project="/home/operator/proj", status="any")
    assert len(anys) == 3


def test_list_goals_rejects_unknown_status(conn: sqlite3.Connection):
    with pytest.raises(ValueError):
        goals_idx.list_goals(conn, status="bogus")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Spin up an MCP server pointed at a tmp DB with the goals schema."""
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(tmp_path))
    # Clear cached server modules so DB_PATH re-resolves.
    for mod in (
        "mcp_server", "mcp_server.server", "mcp_server.tools",
        "mcp_server.resources", "mcp_server.extras",
        "mcp_server.extras.goals_tools",
    ):
        sys.modules.pop(mod, None)

    db_path = tmp_path / "index.db"
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    goals_idx.apply_schema(c)
    yield tmp_path
    try:
        c.close()
    except Exception:
        pass


def test_mcp_get_active_goal_returns_dict(mcp_db_dir: Path, monkeypatch):
    """End-to-end: seed a goal, invoke the MCP tool, expect a dict back."""
    db_path = mcp_db_dir / "index.db"
    t0 = 1_700_000_000
    c = sqlite3.connect(db_path)
    goals_idx.apply_schema(c)
    goals_idx.upsert_from_extractions(
        c, [_ext("goal", "ship goals MCP", t0, cwd="/home/operator/proj")]
    )
    c.commit()
    c.close()

    # Import server fresh so DB_PATH points at our tmp dir.
    import mcp_server.server  # noqa: F401
    tools_mod = importlib.import_module("mcp_server.extras.goals_tools")
    monkeypatch.setenv("PWD", "/home/operator/proj")

    out = tools_mod.get_active_goal(cwd="/home/operator/proj")
    assert isinstance(out, dict)
    assert out["goal_text"] == "ship goals MCP"
    assert out["status"] == "active"


def test_mcp_get_active_goal_returns_none_for_empty_project(mcp_db_dir: Path):
    import mcp_server.server  # noqa: F401
    tools_mod = importlib.import_module("mcp_server.extras.goals_tools")
    out = tools_mod.get_active_goal(cwd="/no/such/project")
    assert out is None


def test_mcp_list_goals_returns_list(mcp_db_dir: Path):
    db_path = mcp_db_dir / "index.db"
    t0 = 1_700_000_000
    c = sqlite3.connect(db_path)
    goals_idx.apply_schema(c)
    goals_idx.upsert_from_extractions(c, [
        _ext("goal", "A", t0, cwd="/home/operator/proj"),
        _ext("goal", "B", t0 + 100, cwd="/home/operator/proj"),
    ])
    c.commit()
    c.close()

    import mcp_server.server  # noqa: F401
    tools_mod = importlib.import_module("mcp_server.extras.goals_tools")
    out = tools_mod.list_goals(cwd="/home/operator/proj", status="active", limit=5)
    assert isinstance(out, list)
    texts = [r["goal_text"] for r in out]
    assert set(texts) == {"A", "B"}


def test_mcp_list_goals_rejects_bad_status(mcp_db_dir: Path):
    import mcp_server.server  # noqa: F401
    tools_mod = importlib.import_module("mcp_server.extras.goals_tools")
    out = tools_mod.list_goals(cwd="/home/operator/proj", status="bogus")
    assert isinstance(out, list)
    assert out and "error" in out[0]


def test_mcp_get_active_goal_when_db_missing(tmp_path: Path, monkeypatch):
    """No index.db at the target path → structured error payload, no raise."""
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(tmp_path))
    for mod in (
        "mcp_server", "mcp_server.server", "mcp_server.tools",
        "mcp_server.resources", "mcp_server.extras",
        "mcp_server.extras.goals_tools",
    ):
        sys.modules.pop(mod, None)

    import mcp_server.server  # noqa: F401
    tools_mod = importlib.import_module("mcp_server.extras.goals_tools")
    out = tools_mod.get_active_goal(cwd="/whatever")
    assert isinstance(out, dict)
    assert "error" in out


# ---------------------------------------------------------------------------
# Sanity: the `time` import is used (kept around for future clock fixtures).
# ---------------------------------------------------------------------------


def test_time_module_available():
    assert callable(_time_mod.time)
