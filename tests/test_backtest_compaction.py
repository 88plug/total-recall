"""Smoke tests for scripts/backtest_compaction.py.

Covers boundary enumeration, the post-boundary time-guard (no post-boundary
content leaks into the proposed packet), and the scoring math on synthetic
fixtures.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# Load the script module by path (scripts/ is not a package).
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backtest_compaction.py"
_spec = importlib.util.spec_from_file_location("backtest_compaction", _SCRIPT)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)  # type: ignore[union-attr]


SID = "22222222-2222-2222-2222-222222222222"
CWD = "/home/op/proj"
TS_PRE = "2025-05-01T12:00:00.000Z"
TS_BOUND = "2025-05-01T12:10:00.000Z"
TS_POST = "2025-05-01T12:20:00.000Z"


def _user(text, uuid, ts=TS_PRE, compact=False):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "isCompactSummary": compact,
        "message": {"role": "user", "content": text},
    }


def _tool(name, inp, tid, uuid, ts=TS_PRE):
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "id": tid, "input": inp}],
        },
    }


def _boundary(ts=TS_BOUND, uuid="bnd"):
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "content": "compacted",
    }


def _write(path: Path, recs) -> str:
    with path.open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return str(path)


def test_enumerate_boundaries(tmp_path):
    recs = [
        _user("a", "u1"),
        _boundary(uuid="b1"),
        _user("b", "u2", ts=TS_POST),
        _boundary(ts=TS_POST, uuid="b2"),
    ]
    path = _write(tmp_path / "s.jsonl", recs)
    found = bt.enumerate_boundaries(path)
    assert [d["idx"] for d in found] == [1, 3]
    assert found[0]["session_id"] == SID
    assert found[0]["cwd"] == CWD


def test_time_guard_no_leak_in_packet(tmp_path):
    # Pre-boundary: one file. Post: a different file. The PROPOSED packet built
    # at the boundary must not mention the post-boundary file.
    recs = [
        _user("work on alpha", "u1"),
        _tool("Read", {"file_path": "/proj/alpha.py"}, "t1", "a1"),
        _boundary(),
        _tool("Read", {"file_path": "/proj/POSTONLY.py"}, "t2", "a2", ts=TS_POST),
    ]
    path = _write(tmp_path / "s.jsonl", recs)
    records = bt._all_records(path)
    positions = bt._boundary_positions(records)
    descs = bt.enumerate_boundaries(path)
    case = bt.score_case(
        records,
        positions[0],
        descs[0]["idx"],
        path,
        SID,
        CWD,
        db_path=None,
        post_window=50,
    )
    # alpha.py is GOLD too? No — alpha is pre-boundary; GOLD is POST only.
    assert case["n_gold_files"] >= 1
    # The packet itself (rebuilt inside score_case) must not see POSTONLY.
    pkt = bt.build_continuation_packet(path, SID, CWD, boundary_idx=descs[0]["idx"])
    assert "POSTONLY" not in json.dumps(pkt)
    assert "alpha.py" in json.dumps(pkt)


def test_file_coverage_math():
    gold = ["/a/one.py", "/b/two.py", "/c/three.py", "/d/four.py"]
    # Baseline mentions one.py (basename) only.
    baseline = "I edited one.py earlier."
    # Combined adds two.py and three.py.
    combined = baseline + " also two.py and three.py"
    assert bt.file_coverage(gold, baseline, 4) == pytest.approx(1 / 4)
    assert bt.file_coverage(gold, combined, 4) == pytest.approx(3 / 4)
    # k clamps to available gold.
    assert bt.file_coverage(gold, combined, 2) == pytest.approx(2 / 2)
    assert bt.file_coverage([], baseline, 5) == 0.0


def test_gold_after_collects_files_and_user_text(tmp_path):
    recs = [
        _boundary(),
        _user("resume: continue the refactor", "u1", ts=TS_POST),
        _tool("Edit", {"file_path": "/proj/x.py"}, "t1", "a1", ts=TS_POST),
        _tool("Bash", {"command": "pytest /proj/tests/test_x.py -q"}, "t2", "a2", ts=TS_POST),
    ]
    path = _write(tmp_path / "s.jsonl", recs)
    records = bt._all_records(path)
    gold = bt.gold_after(records, 0, post_window=50)
    assert "/proj/x.py" in gold["files"]
    # path-like token pulled from the bash command
    assert any("test_x.py" in f for f in gold["files"])
    assert gold["first_user_text"] == "resume: continue the refactor"


def test_rediscovery_and_prevent(tmp_path):
    # Pre-boundary reads alpha.py; post-boundary re-reads alpha.py (rediscovery).
    # Packet (built from pre) contains alpha.py → could_prevent counts it.
    recs = [
        _user("look at alpha", "u1"),
        _tool("Read", {"file_path": "/proj/alpha.py"}, "t1", "a1"),
        _boundary(),
        _tool("Read", {"file_path": "/proj/alpha.py"}, "t2", "a2", ts=TS_POST),
        _tool("Read", {"file_path": "/proj/never_seen.py"}, "t3", "a3", ts=TS_POST),
    ]
    path = _write(tmp_path / "s.jsonl", recs)
    records = bt._all_records(path)
    pos = bt._boundary_positions(records)[0]
    pkt = bt.build_continuation_packet(path, SID, CWD, boundary_idx=2)
    m = bt.rediscovery_metrics(records, pos, post_window=50, packet=pkt)
    assert m["rediscovery"] == 1  # only alpha.py was pre-seen
    assert m["packet_could_prevent"] == 1  # and the packet carried it


def test_run_backtest_end_to_end(tmp_path):
    proj = tmp_path / "projects" / "-home-op-proj"
    proj.mkdir(parents=True)
    recs = [
        _user("build the thing", "u1"),
        _tool("Edit", {"file_path": "/proj/main.py"}, "t1", "a1"),
        _boundary(),
        {**_user("native summary text mentioning main.py", "s1", ts=TS_POST, compact=True)},
        _tool("Edit", {"file_path": "/proj/main.py"}, "t2", "a2", ts=TS_POST),
    ]
    _write(proj / f"{SID}.jsonl", recs)
    out = tmp_path / "out"
    agg = bt.run_backtest(
        projects_root=str(tmp_path / "projects"),
        db_path=None,
        out_dir=str(out),
        limit=None,
        post_window=50,
    )
    assert agg["n_boundaries_seen"] == 1
    assert agg["n_cases_scored"] == 1
    assert (out / "cases.jsonl").exists()
    assert (out / "aggregate.json").exists()
    assert (out / "summary.md").exists()
    # main.py is in both native summary and packet → combined coverage 1.0.
    assert agg["file_coverage"]["k5"]["combined_mean"] == pytest.approx(1.0)
