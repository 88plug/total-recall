"""Unit tests for total_recall.events."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from total_recall import events


@pytest.fixture
def events_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "events.jsonl"


def test_emit_and_read_roundtrip_with_attrs(events_path: Path) -> None:
    events.emit_event(
        "hook.session_start",
        elapsed_ms=12.345,
        session_id="sess-1",
        cwd="/home/x",
        path=events_path,
        custom_attr="hello",
        ignored_none=None,
    )
    events.emit_event(
        "mcp.recall",
        elapsed_ms=4.0,
        session_id="sess-1",
        path=events_path,
    )
    recs = events.read_events(path=events_path)
    assert len(recs) == 2
    r0 = recs[0]
    assert r0["event"] == "hook.session_start"
    assert r0["elapsed_ms"] == 12.345
    assert r0["session_id"] == "sess-1"
    assert r0["cwd"] == "/home/x"
    assert r0["custom_attr"] == "hello"
    assert "ignored_none" not in r0  # None-valued attrs are skipped
    assert "ts" in r0


def test_emit_event_never_raises_with_bad_path(tmp_path: Path) -> None:
    # A path whose parent cannot be created (parent is a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad = blocker / "subdir" / "events.jsonl"
    # Should not raise.
    events.emit_event("hook.test", path=bad)
    # Also a truly absurd path under /dev/null.
    events.emit_event("hook.test", path=Path("/dev/null/nope/events.jsonl"))


def test_emit_event_never_raises_with_unserializable_attr(events_path: Path) -> None:
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    # default=str will stringify; should not raise.
    events.emit_event("hook.weird", path=events_path, obj=Weird())
    recs = events.read_events(path=events_path)
    assert recs and recs[0]["event"] == "hook.weird"


def test_emit_event_swallows_circular_attr(events_path: Path, caplog) -> None:
    # Circular reference is unserializable even with default=str -> covered by the
    # outer try/except. Just ensure no exception escapes.
    a: dict = {}
    a["self"] = a
    events.emit_event("hook.cycle", path=events_path, ref=a)
    # Either the line was written (default=str handles it) or it was swallowed.
    # Both outcomes are acceptable; the contract is "never raises".


def test_rotation_triggers_when_exceeding_max(
    events_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink MAX so exactly one rotation happens during the run. Each event is
    # ~150 bytes; 20_000 bytes -> rotate after ~130 events; 200 events total
    # leaves a manageable live tail. We size MAX so all 200 events survive
    # across live + one rotation (KEEP_ROTATIONS=3 is plenty).
    monkeypatch.setattr(events, "MAX_SIZE_BYTES", 20_000)
    for i in range(200):
        events.emit_event("hook.spam", path=events_path, i=i, payload="x" * 32)

    rotated_1 = events_path.with_suffix(".jsonl.1.gz")
    assert rotated_1.exists(), "expected .1.gz rotation file"

    # Live file should be smaller than what we wrote in total.
    assert events_path.stat().st_size < 200 * 200

    # All events readable across live + rotated.
    recs = events.read_events(path=events_path)
    assert len(recs) == 200
    # Chronologically sorted by ts.
    timestamps = [r["ts"] for r in recs]
    assert timestamps == sorted(timestamps)
    # Indices preserved in chronological order since timestamps are monotonic
    # at microsecond resolution for this small loop.
    indices = [r["i"] for r in recs]
    assert indices == sorted(indices)


def test_rotation_keeps_only_n_rotations(
    events_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events, "MAX_SIZE_BYTES", 256)
    # Generate enough volume to trigger multiple rotations.
    for i in range(1000):
        events.emit_event("hook.bulk", path=events_path, i=i, payload="y" * 64)

    # Older-than-KEEP must not exist.
    too_old = events_path.with_suffix(f".jsonl.{events.KEEP_ROTATIONS + 1}.gz")
    assert not too_old.exists()
    # At least .1.gz exists.
    assert events_path.with_suffix(".jsonl.1.gz").exists()


def test_rotated_file_is_valid_gzip_ndjson(
    events_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events, "MAX_SIZE_BYTES", 256)
    for i in range(50):
        events.emit_event("hook.gz", path=events_path, i=i, payload="z" * 32)
    rotated = events_path.with_suffix(".jsonl.1.gz")
    assert rotated.exists()
    # Each line must be valid JSON.
    with gzip.open(rotated, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            assert rec["event"] == "hook.gz"


def test_summarize_per_event_percentiles(events_path: Path) -> None:
    # 'a': elapsed 1..100 (p50=50, p95=95, max=100)
    for i in range(1, 101):
        events.emit_event("a", elapsed_ms=float(i), path=events_path)
    # 'b': elapsed 10..20, one with error
    for i in range(10, 21):
        events.emit_event("b", elapsed_ms=float(i), path=events_path)
    events.emit_event("b", elapsed_ms=999.0, error="boom", path=events_path)

    summary = events.summarize_events(path=events_path)
    assert summary["total"] == 100 + 11 + 1
    assert summary["error_count"] == 1

    a = summary["by_event"]["a"]
    assert a["count"] == 100
    assert a["errors"] == 0
    # Nearest-rank percentile: p50 of 1..100 -> 50; p95 -> 95; max -> 100.
    assert a["p50_ms"] == 50.0
    assert a["p95_ms"] == 95.0
    assert a["max_ms"] == 100.0

    b = summary["by_event"]["b"]
    assert b["count"] == 12
    assert b["errors"] == 1
    assert b["max_ms"] == 999.0


def test_summarize_empty_when_no_file(tmp_path: Path) -> None:
    summary = events.summarize_events(path=tmp_path / "missing.jsonl")
    assert summary == {"total": 0, "error_count": 0, "by_event": {}}


def test_read_events_since_ts(events_path: Path) -> None:
    import time

    events.emit_event("old", path=events_path)
    # Sleep just long enough that subsequent ISO timestamps differ.
    time.sleep(0.01)
    cutoff = time.time()
    time.sleep(0.01)
    events.emit_event("new", path=events_path)

    recs = events.read_events(path=events_path, since_ts=cutoff)
    names = [r["event"] for r in recs]
    assert "new" in names
    assert "old" not in names


def test_read_events_handles_malformed_lines(events_path: Path) -> None:
    events.emit_event("good", path=events_path)
    # Append garbage.
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write("\n")  # blank
        fh.write('{"event": "manual", "ts": "2025-01-01T00:00:00+00:00"}\n')
    recs = events.read_events(path=events_path)
    names = sorted(r["event"] for r in recs)
    assert names == ["good", "manual"]


def test_default_path_under_claude_plugin_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Sanity: the default path constant was assembled from CLAUDE_PLUGIN_DATA at
    # import time, so we cannot retroactively change it. But we can confirm the
    # structure: it ends with total-recall/logs/events.jsonl.
    p = events.DEFAULT_EVENTS_PATH
    assert p.name == "events.jsonl"
    assert p.parent.name == "logs"
    assert p.parent.parent.name == "total-recall"
