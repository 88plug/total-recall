"""Unit tests for extractors.continuation_packet.

Synthetic JSONL fixtures exercise every packet field, the boundary_idx
window cut, the budget eviction + truncation, tool_use→tool_result pairing,
the time-guard (no post-boundary index leakage), and the no-db path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from extractors.continuation_packet import (
    _apply_budget,
    _iso_to_epoch,
    _read_records,
    build_continuation_packet,
    render_continuation_packet,
)

TS_PRE = "2025-05-01T12:00:00.000Z"
TS_PRE2 = "2025-05-01T12:05:00.000Z"
TS_BOUND = "2025-05-01T12:10:00.000Z"
TS_POST = "2025-05-01T12:20:00.000Z"

SID = "11111111-1111-1111-1111-111111111111"
CWD = "/home/op/proj"


def _user(text, uuid, ts=TS_PRE, meta=False, compact=False):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "isMeta": meta,
        "isCompactSummary": compact,
        "message": {"role": "user", "content": text},
    }


def _assistant_text(text, uuid, ts=TS_PRE):
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _assistant_tool(name, inp, tool_id, uuid, ts=TS_PRE):
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "id": tool_id, "input": inp}],
        },
    }


def _tool_result(tool_id, content, uuid, is_error=False, ts=TS_PRE):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": is_error,
                    "content": content,
                }
            ],
        },
    }


def _boundary(uuid="bnd", ts=TS_BOUND):
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "content": "Conversation compacted",
    }


def _write_jsonl(path: Path, records: list[dict]) -> str:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Tail-derived fields
# ---------------------------------------------------------------------------


def test_all_tail_fields(tmp_path):
    recs = [
        _user("set up the relay deploy", "u1"),
        _assistant_text("Working on it.", "a1"),
        _assistant_tool("Read", {"file_path": "/home/op/proj/relay.py"}, "t1", "a2"),
        _tool_result("t1", "file contents...", "r1"),
        _assistant_tool("Edit", {"file_path": "/home/op/proj/relay.py"}, "t2", "a3"),
        _tool_result("t2", "edit applied", "r2"),
        _user("now run the tests please", "u2", ts=TS_PRE2),
        _assistant_tool("Bash", {"command": "pytest -q tests/ && echo done"}, "t3", "a4"),
        _tool_result("t3", "1 failed, traceback...", "r3", is_error=True),
        _assistant_tool(
            "TodoWrite",
            {"todos": [{"content": "fix the failing test", "status": "in_progress"}]},
            "t4",
            "a5",
        ),
        _assistant_text("Next: fix the assertion in test_relay. Let me re-run the suite.", "a6"),
    ]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))

    pkt = build_continuation_packet(path, SID, CWD, db_path=None, max_chars=4000)

    assert pkt["last_user_directive"] == "now run the tests please"

    files = pkt["files_in_flight"]
    assert files[0]["path"] == "/home/op/proj/relay.py"
    # Read + Edit on same file → count 2, most recent verb is Edit.
    assert files[0]["count"] == 2
    assert files[0]["verb"] == "Edit"

    acts = pkt["last_actions"]
    assert len(acts) == 3
    # Most recent first: TodoWrite is the newest tool_use, then the failed
    # pytest Bash (error result → ok False), then the Edit.
    assert acts[0]["tool"] == "TodoWrite"
    bash = next(a for a in acts if a["tool"] == "Bash")
    assert bash["arg"].startswith("pytest -q")
    assert bash["ok"] is False

    # open_plan came from the TodoWrite input.
    assert isinstance(pkt["open_plan"], list)
    assert pkt["open_plan"][0]["content"] == "fix the failing test"

    assert "re-run the suite" in pkt["next_step"]
    assert pkt["_kind"] == "continuation_packet"


def test_skips_meta_and_compact_for_directive(tmp_path):
    recs = [
        _user("real directive here", "u1"),
        _user("local-command noise", "u2", meta=True),
        _user("This session is being continued ...", "u3", compact=True),
        # a tool_result-only user record must also be skipped
        _tool_result("tX", "result text", "r1"),
    ]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    pkt = build_continuation_packet(path, SID, CWD)
    assert pkt["last_user_directive"] == "real directive here"


def test_open_plan_text_fallback(tmp_path):
    recs = [
        _user("go", "u1"),
        _assistant_text("Plan: first do A, then B, finally C.", "a1"),
    ]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    pkt = build_continuation_packet(path, SID, CWD)
    assert "Plan:" in pkt["open_plan"]


# ---------------------------------------------------------------------------
# boundary_idx window cut
# ---------------------------------------------------------------------------


def test_boundary_idx_window_cut(tmp_path):
    pre = [
        _user("pre-boundary directive", "u1"),
        _assistant_tool("Read", {"file_path": "/pre/file.py"}, "t1", "a1"),
    ]
    boundary = [_boundary()]
    post = [
        _user("POST boundary directive — must not leak", "u9", ts=TS_POST),
        _assistant_tool("Read", {"file_path": "/post/leak.py"}, "t9", "a9", ts=TS_POST),
    ]
    recs = pre + boundary + post
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))

    # boundary is at physical line index len(pre) == 2.
    pkt = build_continuation_packet(path, SID, CWD, boundary_idx=len(pre))
    assert pkt["last_user_directive"] == "pre-boundary directive"
    blob = json.dumps(pkt)
    assert "/post/leak.py" not in blob
    assert "POST boundary" not in blob
    assert "/pre/file.py" in blob


def test_read_records_honors_boundary(tmp_path):
    recs = [_user("a", "u1"), _user("b", "u2"), _boundary(), _user("c", "u3")]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    got = _read_records(str(path), boundary_idx=2)
    assert len(got) == 2
    assert all(r.get("type") == "user" for r in got)


# ---------------------------------------------------------------------------
# Tool pairing edge cases
# ---------------------------------------------------------------------------


def test_tool_pair_unpaired_is_ok(tmp_path):
    # A tool_use with no matching result (cut off at compaction) → ok True.
    recs = [
        _user("go", "u1"),
        _assistant_tool("Bash", {"command": "long running thing"}, "t1", "a1"),
    ]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    pkt = build_continuation_packet(path, SID, CWD)
    assert pkt["last_actions"][0]["ok"] is True


def test_tool_result_error_text_marks_not_ok(tmp_path):
    recs = [
        _user("go", "u1"),
        _assistant_tool("Bash", {"command": "do it"}, "t1", "a1"),
        # is_error False but content mentions error → not ok
        _tool_result("t1", "Traceback (most recent call last): boom", "r1", is_error=False),
    ]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    pkt = build_continuation_packet(path, SID, CWD)
    assert pkt["last_actions"][0]["ok"] is False


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_evicts_tail_priority():
    fields = {
        "active_goal": "G" * 50,
        "last_user_directive": "D" * 50,
        "files_in_flight": [{"path": "/x", "verb": "Read", "count": 1}],
        "failed_attempts_this_session": ["F" * 200],
        "next_step": "N" * 200,
    }
    out = _apply_budget(dict(fields), max_chars=140)
    # Highest-priority survives, lowest-priority evicted.
    assert "active_goal" in out
    assert "failed_attempts_this_session" not in out
    assert "next_step" not in out
    assert len(json.dumps(out, separators=(",", ":"))) <= 140


def test_budget_truncates_single_huge_field():
    fields = {"active_goal": "Z" * 5000}
    out = _apply_budget(dict(fields), max_chars=200)
    assert "active_goal" in out
    # The builder serializes with ensure_ascii=False (its own _serialize).
    serialized = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= 200
    assert out["active_goal"].endswith("…")


def test_iso_to_epoch():
    assert _iso_to_epoch("2025-05-01T12:00:00.000Z") == 1746100800
    assert _iso_to_epoch("1746100800") == 1746100800
    assert _iso_to_epoch(1746100800) == 1746100800
    assert _iso_to_epoch(None) is None
    assert _iso_to_epoch("not a date") is None


# ---------------------------------------------------------------------------
# Index-derived lane + time-guard
# ---------------------------------------------------------------------------


def _seed_index(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY, kind TEXT, content TEXT, session_id TEXT,
            cwd TEXT, ts INTEGER, source_uuid TEXT, score REAL DEFAULT 0.5,
            scope TEXT, context_json TEXT
        );
        CREATE TABLE goal_stack (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, goal_text TEXT NOT NULL,
            declared_ts INTEGER NOT NULL, last_progress_ts INTEGER,
            status TEXT NOT NULL DEFAULT 'active', related_projects TEXT,
            source_session TEXT, UNIQUE(project, goal_text)
        );
        """
    )
    pre = _iso_to_epoch(TS_PRE)
    post = _iso_to_epoch(TS_POST)
    conn.execute(
        "INSERT INTO goal_stack(project, goal_text, declared_ts, status) VALUES (?,?,?,?)",
        (CWD, "ship the relay refactor", pre, "active"),
    )
    rows = [
        ("standing_decision", "always use provider-y", SID, CWD, pre, 0.9),
        ("model_correction", "do not touch prod config", SID, CWD, pre, 0.8),
        ("failed_attempt", "tried provider-x, it 500'd", SID, CWD, pre, 0.7),
        # POST-boundary row — must be excluded by the time-guard.
        ("standing_decision", "LEAKED post-boundary decision", SID, CWD, post, 0.99),
        # Different session — must be excluded.
        ("standing_decision", "other session decision", "other-sid", CWD, pre, 0.95),
    ]
    conn.executemany(
        "INSERT INTO extractions(kind, content, session_id, cwd, ts, score) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db_path)


def test_index_fields_and_time_guard(tmp_path):
    db = _seed_index(tmp_path / "index.db")
    recs = [_user("pre directive", "u1"), _boundary()]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))

    pkt = build_continuation_packet(path, SID, CWD, db_path=db, boundary_idx=1, max_chars=4000)
    # goal from goal_stack
    assert pkt["active_goal"] == "ship the relay refactor"
    decisions = pkt["decisions_this_session"]
    assert "always use provider-y" in decisions
    assert "do not touch prod config" in decisions
    # time-guard + session filter
    blob = json.dumps(pkt)
    assert "LEAKED post-boundary" not in blob
    assert "other session" not in blob
    assert pkt["failed_attempts_this_session"] == ["tried provider-x, it 500'd"]


def test_active_goal_away_summary_fallback(tmp_path):
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY, kind TEXT, content TEXT, session_id TEXT,
            cwd TEXT, ts INTEGER, score REAL DEFAULT 0.5
        );
        CREATE TABLE goal_stack (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, goal_text TEXT NOT NULL,
            declared_ts INTEGER NOT NULL, last_progress_ts INTEGER,
            status TEXT NOT NULL DEFAULT 'active', UNIQUE(project, goal_text)
        );
        """
    )
    conn.execute(
        "INSERT INTO extractions(kind, content, session_id, cwd, ts) VALUES (?,?,?,?,?)",
        (
            "away_summary",
            "Refactoring the indexer for speed.\nSecond line.",
            SID,
            CWD,
            _iso_to_epoch(TS_PRE),
        ),
    )
    conn.commit()
    conn.close()

    recs = [_user("go", "u1"), _boundary()]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    pkt = build_continuation_packet(path, SID, CWD, db_path=str(db_path), boundary_idx=1)
    # falls back to first line of most recent away_summary
    assert pkt["active_goal"] == "Refactoring the indexer for speed."


def test_no_db_path_omits_index_fields(tmp_path):
    recs = [_user("just a directive", "u1"), _assistant_text("done.", "a1")]
    path = Path(_write_jsonl(tmp_path / "s.jsonl", recs))
    pkt = build_continuation_packet(path, SID, CWD, db_path=None)
    assert "active_goal" not in pkt
    assert "decisions_this_session" not in pkt
    assert "failed_attempts_this_session" not in pkt
    assert "last_user_directive" in pkt


def test_never_raises_on_bad_inputs(tmp_path):
    # Nonexistent transcript + nonexistent db → empty-ish packet, no exception.
    pkt = build_continuation_packet(
        str(tmp_path / "nope.jsonl"), SID, CWD, db_path=str(tmp_path / "nope.db")
    )
    assert pkt["_kind"] == "continuation_packet"


def test_garbage_lines_tolerated(tmp_path):
    path = tmp_path / "s.jsonl"
    with path.open("w") as fh:
        fh.write("not json\n")
        fh.write(json.dumps(_user("good directive", "u1")) + "\n")
        fh.write("\n")
        fh.write("{broken json\n")
    pkt = build_continuation_packet(str(path), SID, CWD)
    assert pkt["last_user_directive"] == "good directive"


def test_render_continuation_packet_basic():
    packet = {
        "active_goal": {"goal": "ship compaction continuity"},
        "last_user_directive": "fix the failing assertion in parser.py",
        "files_in_flight": [{"path": "/p/parser.py", "verb": "Edit"}],
        "last_actions": [{"tool": "Bash", "arg": "pytest", "ok": False}],
        "_kind": "continuation_packet",
    }
    out = render_continuation_packet(packet, max_chars=6000)
    assert "Active goal: ship compaction continuity" in out
    assert "fix the failing assertion in parser.py" in out
    assert "/p/parser.py" in out
    assert "[FAILED] Bash" in out
    # Priority order: active_goal renders before last_user_directive.
    assert out.index("Active goal") < out.index("Last directive")


def test_render_continuation_packet_empty_and_bad():
    assert render_continuation_packet({}, max_chars=6000) == ""
    assert render_continuation_packet({"_kind": "continuation_packet"}) == ""
    # Non-dict input never raises.
    assert render_continuation_packet(None) == ""  # type: ignore[arg-type]


def test_render_continuation_packet_respects_cap():
    packet = {
        "last_user_directive": "x" * 500,
        "open_plan": "y" * 5000,
        "_kind": "continuation_packet",
    }
    out = render_continuation_packet(packet, max_chars=300)
    assert len(out) <= 300
