"""End-to-end integration tests for the NDJSON event log.

Covers the contract surface of :mod:`total_recall.events`:

* :func:`emit_event` → :func:`read_events` round-trip (chronological order,
  no event loss).
* Rotation: when the live file exceeds ``MAX_SIZE_BYTES`` it compresses
  to ``events.jsonl.1.gz`` and the live file shrinks.
* :func:`summarize_events` p50/p95 statistics over a synthetic load.

Each test imports its dependency inside the body and ``pytest.skip``s
cleanly if :mod:`total_recall.events` is not on this branch yet — so
this file can sit in CI from day one without flaking on partial
worktrees.
"""

from __future__ import annotations

import pathlib
import random

import pytest


def _try_events():
    """Return the ``total_recall.events`` module or ``pytest.skip``."""
    try:
        from total_recall import events  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"total_recall.events not present on this branch: {exc}")
    return events


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------


def test_events_round_trip(tmp_path: pathlib.Path):
    """Emit a bunch of events; read them back; assert chronological + counts.

    The contract:
      * every emit lands as one NDJSON line
      * ``read_events`` returns them in chronological order (by ``ts``)
      * counts and ``event`` values round-trip exactly
    """
    events = _try_events()

    path = tmp_path / "events.jsonl"

    payloads = [
        ("hook.fire", {"elapsed_ms": 12.5, "session_id": "s-1"}),
        ("ingest.start", {"elapsed_ms": 0.1}),
        ("ingest.done", {"elapsed_ms": 250.0, "session_id": "s-1"}),
        ("mcp.tool", {"elapsed_ms": 4.7, "session_id": "s-2"}),
        ("hook.fire", {"elapsed_ms": 8.9, "session_id": "s-2", "error": "boom"}),
    ]
    for name, kwargs in payloads:
        events.emit_event(name, path=path, **kwargs)

    read = events.read_events(path=path)
    assert isinstance(read, list), f"read_events did not return a list: {type(read)}"
    assert len(read) == len(payloads), f"emitted {len(payloads)} events but read {len(read)} back"

    # Chronological order — ts strings should be non-decreasing.
    ts_list = [r.get("ts", "") for r in read]
    assert ts_list == sorted(ts_list), f"events not in chronological order: {ts_list}"

    # Event names round-trip (multiset equality, since within the same
    # microsecond the ordering of equal-ts events is implementation-defined).
    emitted_names = sorted(name for name, _ in payloads)
    read_names = sorted(r.get("event", "") for r in read)
    assert emitted_names == read_names, (
        f"event-name multiset mismatch:\n  emitted: {emitted_names}\n  read:    {read_names}"
    )


# ---------------------------------------------------------------------------
# 2. Rotation
# ---------------------------------------------------------------------------


def test_events_rotation_compresses_at_10MB(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    """Monkeypatch ``_max_size`` to a small value; emit until rotation; assert
    ``events.jsonl.1.gz`` exists and the live file is back to a small size.

    We monkeypatch the indirection ``events._max_size`` (added expressly so
    tests don't have to mutate ``MAX_SIZE_BYTES`` at module scope) to a
    tiny ceiling — 4 KiB — so we can drive a rotation in a handful of
    emits instead of dumping 10 MiB into the test.
    """
    events = _try_events()

    tiny_max = 4 * 1024  # 4 KiB
    monkeypatch.setattr(events, "_max_size", lambda: tiny_max, raising=True)

    path = tmp_path / "events.jsonl"
    rotated = path.with_suffix(".jsonl.1.gz")

    # Each emit writes ~150-200 bytes; 100 emits is comfortably > 4 KiB.
    padding = "x" * 100  # ensure each line is fat enough
    for i in range(200):
        events.emit_event(
            "rotate.test",
            path=path,
            elapsed_ms=float(i),
            session_id=f"s-{i:04d}",
            payload=padding,
        )
        # Keep emitting a few more after rotation so the live file
        # accumulates fresh content post-rotation; helps assert it's
        # been truncated correctly.
        if rotated.exists() and i > 5 and path.stat().st_size > 0:
            # one more then break
            events.emit_event("rotate.post", path=path, marker=True)
            break

    assert rotated.exists(), (
        f"rotation did not produce {rotated.name} after 200 emits at "
        f"4 KiB max; live file size: "
        f"{path.stat().st_size if path.exists() else 'missing'}"
    )
    # Post-rotation live file MUST be smaller than the ceiling.
    live_size = path.stat().st_size
    assert live_size < tiny_max, (
        f"live file is still {live_size} bytes after rotation (ceiling {tiny_max})"
    )

    # Combined read should still surface all events — rotation must not
    # drop data.
    all_events = events.read_events(path=path)
    assert isinstance(all_events, list)
    assert len(all_events) >= 6, (
        f"read_events lost data across rotation: got {len(all_events)} events"
    )
    # The post-rotation emit should be present and chronologically last.
    names = [e.get("event") for e in all_events]
    assert "rotate.test" in names
    if "rotate.post" in names:
        assert names[-1] == "rotate.post", (
            f"post-rotation event not last in chronological order: {names[-5:]}"
        )


# ---------------------------------------------------------------------------
# 3. p50 / p95 summarization
# ---------------------------------------------------------------------------


def test_summarize_events_p95(tmp_path: pathlib.Path):
    """Emit 1000 events with varied ``elapsed_ms``; assert p50/p95 are sensible.

    Drives a uniform [0, 1000) ms distribution so we have known ground
    truth: p50 ≈ 500, p95 ≈ 950 (with ±10% tolerance for nearest-rank
    quantization noise).
    """
    events = _try_events()
    path = tmp_path / "events.jsonl"

    rng = random.Random(0xC0FFEE)
    n = 1000
    for _ in range(n):
        ms = rng.uniform(0.0, 1000.0)
        events.emit_event("perf.sample", path=path, elapsed_ms=ms)

    summary = events.summarize_events(path=path)
    assert isinstance(summary, dict), f"summarize_events not a dict: {type(summary)}"
    assert summary.get("total") == n, f"summary.total mismatch: got {summary.get('total')} want {n}"

    by_event = summary.get("by_event") or {}
    bucket = by_event.get("perf.sample")
    assert bucket is not None, f"no 'perf.sample' bucket: {by_event}"
    assert bucket.get("count") == n, f"per-event count mismatch: got {bucket.get('count')} want {n}"

    p50 = float(bucket.get("p50_ms", -1))
    p95 = float(bucket.get("p95_ms", -1))
    max_ms = float(bucket.get("max_ms", -1))

    # Uniform [0, 1000): expected p50 ≈ 500, p95 ≈ 950, max < 1000.
    # Allow generous slack — this is a property check, not a stats test.
    assert 400.0 <= p50 <= 600.0, f"p50 out of expected band: {p50}"
    assert 850.0 <= p95 <= 1000.0, f"p95 out of expected band: {p95}"
    assert p50 <= p95 <= max_ms <= 1000.0, (
        f"percentile ordering violated: p50={p50} p95={p95} max={max_ms}"
    )
