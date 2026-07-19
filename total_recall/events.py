"""NDJSON event logging for total-recall.

Every hook invocation, MCP tool call, and ingest run can append a JSON line
to events.jsonl. Used by `total-recall metrics health` to summarize hook
fire rate, p95 latency, error count.

Design notes:
- Append-only NDJSON, one event per line.
- 10MB rotation: when events.jsonl exceeds 10MB, rotate to events.jsonl.1.gz
  and start fresh. Keep the most recent 3 rotated files.
- Best-effort: emit_event() must NEVER raise — observability code that
  breaks the host process is anti-pattern.
- Thread/process safe enough: O_APPEND on POSIX guarantees atomic appends
  for lines < PIPE_BUF. We're well under that.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DATA_ROOT = os.environ.get("GROK_PLUGIN_DATA") or os.environ.get(
    "CLAUDE_PLUGIN_DATA", "~/.local/share/total-recall"
)
DEFAULT_EVENTS_PATH = Path(_DATA_ROOT).expanduser() / "total-recall" / "logs" / "events.jsonl"

MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
KEEP_ROTATIONS = 3


def emit_event(
    event: str,
    *,
    elapsed_ms: float | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
    error: str | None = None,
    path: Path = DEFAULT_EVENTS_PATH,
    **attrs: Any,
) -> None:
    """Append one NDJSON line. Best-effort: any failure is logged at DEBUG and swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if elapsed_ms is not None:
            record["elapsed_ms"] = round(float(elapsed_ms), 3)
        if session_id:
            record["session_id"] = session_id
        if cwd:
            record["cwd"] = cwd
        if error:
            record["error"] = error
        for k, v in attrs.items():
            if v is not None:
                record[k] = v
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        log.debug("events.emit_event failed (swallowed): %s", exc)


def _rotate_if_needed(path: Path) -> None:
    """If file exceeds MAX_SIZE_BYTES, compress to .1.gz and shift older rotations."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    except OSError:
        return
    if size < _max_size():
        return
    try:
        # Shift .1.gz -> .2.gz -> ... up to KEEP_ROTATIONS; drop oldest.
        for i in range(KEEP_ROTATIONS - 1, 0, -1):
            src = path.with_suffix(f".jsonl.{i}.gz")
            dst = path.with_suffix(f".jsonl.{i + 1}.gz")
            if src.exists():
                src.replace(dst)
        # Compress current -> .1.gz.
        rotated = path.with_suffix(".jsonl.1.gz")
        with open(path, "rb") as src, gzip.open(rotated, "wb") as dst:
            shutil.copyfileobj(src, dst)
        # Truncate live file.
        path.write_text("", encoding="utf-8")
        # Drop oldest beyond keep.
        oldest = path.with_suffix(f".jsonl.{KEEP_ROTATIONS + 1}.gz")
        if oldest.exists():
            oldest.unlink()
    except Exception as exc:
        log.debug("events.rotate failed (swallowed): %s", exc)


def _max_size() -> int:
    """Indirection so tests can monkeypatch MAX_SIZE_BYTES at module level."""
    return MAX_SIZE_BYTES


def _parse_ts(ts: str) -> float:
    """Parse ISO-8601 timestamp to epoch seconds. Returns 0.0 on failure."""
    try:
        # Python 3.11+ handles 'Z' suffix; be defensive for earlier.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _iter_lines(p: Path):
    """Yield decoded JSON lines from a live or .gz file. Skips malformed lines."""
    if not p.exists():
        return
    try:
        if p.suffix == ".gz":

            def opener():
                return gzip.open(p, "rt", encoding="utf-8")
        else:

            def opener():
                return open(p, encoding="utf-8")

        with opener() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def read_events(
    path: Path = DEFAULT_EVENTS_PATH,
    since_ts: float | None = None,
) -> list[dict]:
    """Read events from live + rotated files. Returns chronologically sorted list.

    If ``since_ts`` is provided (epoch seconds), only events with a parseable
    ``ts`` >= since_ts are returned.
    """
    events: list[dict] = []
    candidates: list[Path] = []
    # Rotated files first (older), then live.
    for i in range(KEEP_ROTATIONS, 0, -1):
        candidates.append(path.with_suffix(f".jsonl.{i}.gz"))
    candidates.append(path)
    for p in candidates:
        for rec in _iter_lines(p):
            if since_ts is not None:
                rec_ts = rec.get("ts")
                if not isinstance(rec_ts, str) or _parse_ts(rec_ts) < since_ts:
                    continue
            events.append(rec)
    events.sort(key=lambda r: _parse_ts(r.get("ts", "")) if isinstance(r.get("ts"), str) else 0.0)
    return events


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile on a sorted-ascending list. Returns 0.0 if empty."""
    if not values:
        return 0.0
    s = sorted(values)
    # Nearest-rank: index = ceil(pct/100 * N) - 1, clamped.
    import math

    idx = max(0, min(len(s) - 1, math.ceil(pct / 100.0 * len(s)) - 1))
    return s[idx]


def summarize_events(
    path: Path = DEFAULT_EVENTS_PATH,
    since_ts: float | None = None,
) -> dict:
    """Aggregate: per-event counts, p50/p95/max elapsed_ms per event, error_count.

    Returns:
        {
          "total": int,
          "error_count": int,
          "by_event": {
            "<event_name>": {
              "count": int,
              "errors": int,
              "p50_ms": float,
              "p95_ms": float,
              "max_ms": float,
            },
            ...
          }
        }
    """
    events = read_events(path=path, since_ts=since_ts)
    by_event: dict[str, dict[str, Any]] = {}
    elapsed_by_event: dict[str, list[float]] = {}
    error_count = 0
    for rec in events:
        name = rec.get("event")
        if not isinstance(name, str):
            continue
        bucket = by_event.setdefault(
            name, {"count": 0, "errors": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        )
        bucket["count"] += 1
        if rec.get("error"):
            bucket["errors"] += 1
            error_count += 1
        ms = rec.get("elapsed_ms")
        if isinstance(ms, (int, float)):
            elapsed_by_event.setdefault(name, []).append(float(ms))
    for name, samples in elapsed_by_event.items():
        bucket = by_event[name]
        bucket["p50_ms"] = round(_percentile(samples, 50), 3)
        bucket["p95_ms"] = round(_percentile(samples, 95), 3)
        bucket["max_ms"] = round(max(samples), 3)
    return {
        "total": len(events),
        "error_count": error_count,
        "by_event": by_event,
    }
