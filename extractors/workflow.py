"""Workflow-profile extractor.

Models HOW the operator works — their execution style, autonomy level,
fan-out vocabulary, planning idiom, peak working hours, and subagent
adoption rate. Complements the voice/operator profiles (which cover WHO
and HOW they talk) with a behavioural fingerprint of the session shape.

All signal is derived heuristically from raw JSONL records. No LLM, no
new dependencies beyond the stdlib. Tolerant of malformed input — bad
lines are silently skipped.

Stored in the ``workflow_profile`` table (see :mod:`index.workflow`) so
the MCP layer can serve it cheaply at session start.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "WorkflowProfile",
    "extract_workflow",
    "extract_workflow_incremental",
]


# ---------------------------------------------------------------------------
# WorkflowProfile dataclass
# ---------------------------------------------------------------------------


@dataclass
class WorkflowProfile:
    """Behavioural fingerprint of how the operator drives Claude Code sessions.

    All fields are computed purely from session data — no operator-specific
    literals are hardcoded here.
    """

    # Average number of fan-out-style requests per session.
    fan_out_hits_per_session: float = 0.0

    # Operator's actual fan-out phrases in frequency order.
    fan_out_vocabulary: list[str] = field(default_factory=list)

    # Median requested agent/task count from "use N agents" patterns; 0 = no signal.
    typical_agent_count: int = 0

    # 0.0 = confirms every step; 1.0 = pure fire-and-forget.
    autonomy_score: float = 0.0

    # Average number of mid-flight queued-command interrupts per session.
    interrupt_rate: float = 0.0

    # Preferred planning vocabulary: "" or one of "waves"/"phases"/"steps"/"sprints".
    planning_idiom: str = ""

    # Top-3 hours of day (0..23) by message volume.
    peak_hours: list[int] = field(default_factory=list)

    # Broad work-window label derived from peak_hours distribution.
    preferred_work_window: str = "mixed"

    # Distribution shape of user messages per session.
    session_shape: str = "mixed"

    # Fraction of sessions that contained TaskCreate / agent tool use.
    subagent_adoption: float = 0.0

    # Number of sessions analysed (provides confidence signal to consumers).
    sample_size: int = 0


# ---------------------------------------------------------------------------
# Tunables — no operator-specific literals
# ---------------------------------------------------------------------------

# Phrases that indicate fan-out / parallel execution requests.
# Ordered from most specific to least so the vocabulary list is meaningful.
_FAN_OUT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fan out", re.compile(r"\bfan[\s\-]out\b", re.IGNORECASE)),
    ("spin up", re.compile(r"\bspin[\s\-]up\b", re.IGNORECASE)),
    ("in parallel", re.compile(r"\bin parallel\b", re.IGNORECASE)),
    ("use .* agents", re.compile(r"\buse\s+\d+\s+agents?\b", re.IGNORECASE)),
    ("full send", re.compile(r"\bfull[\s\-]send\b", re.IGNORECASE)),
    ("fan-out", re.compile(r"\bfan-out\b", re.IGNORECASE)),  # hyphenated variant
]

# Pattern to capture the agent count from "use N agents" style phrases.
_AGENT_COUNT_RE = re.compile(r"\buse\s+(\d+)\s+agents?\b", re.IGNORECASE)

# One-word autonomy turns — low-friction "keep going" signals.
_AUTONOMY_WORDS: frozenset[str] = frozenset(
    {"yes", "ok", "go", "okay", "yep", "do", "continue", "sure", "proceed", "ship"}
)
# Short phrases (2–3 tokens) that also signal autonomy / fire-and-forget.
_AUTONOMY_PHRASES: tuple[str, ...] = (
    "do it", "full send", "go ahead", "just do it", "run it", "send it",
    "fire away", "make it so", "just go", "keep going",
)

# Planning-idiom keywords in priority order (first match wins per session).
_PLANNING_IDIOMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("waves",   re.compile(r"\bwaves?\b", re.IGNORECASE)),
    ("phases",  re.compile(r"\bphases?\b", re.IGNORECASE)),
    ("steps",   re.compile(r"\bsteps?\b", re.IGNORECASE)),
    ("sprints", re.compile(r"\bsprints?\b", re.IGNORECASE)),
)

# Interrupt record types used by Claude Code when the user queues a command
# mid-flight.
_INTERRUPT_TYPES: frozenset[str] = frozenset({"queue-operation"})

# Tool names that indicate subagent / task delegation.
_SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset(
    {"TaskCreate", "Task", "task_create", "dispatch_agent"}
)

# Session shape thresholds (user messages per session).
_OPS_BURST_MAX = 10   # short-ops sessions
_MARATHON_MIN = 200   # marathon sessions


# ---------------------------------------------------------------------------
# Per-record helpers
# ---------------------------------------------------------------------------


def _record_type(rec: Any) -> str:
    """Return the ``type`` field from a dict or object, or ""."""
    if isinstance(rec, dict):
        return str(rec.get("type") or "")
    return str(getattr(rec, "type", "") or "")


def _record_timestamp(rec: Any) -> str | None:
    """Return the timestamp string from a record, or None."""
    if isinstance(rec, dict):
        return rec.get("timestamp") or rec.get("ts")
    return getattr(rec, "timestamp", None) or getattr(rec, "ts", None)


def _record_session_id(rec: Any) -> str | None:
    if isinstance(rec, dict):
        return rec.get("sessionId") or rec.get("session_id")
    return getattr(rec, "sessionId", None) or getattr(rec, "session_id", None)


def _user_text(rec: Any) -> str | None:
    """Extract plain text from a user turn.  Returns None for non-user records."""
    if isinstance(rec, dict):
        if rec.get("type") != "user":
            return None
        msg = rec.get("message") or {}
        if isinstance(msg, dict):
            content = msg.get("content")
        else:
            content = None
        # content can be str or list
        if isinstance(content, str):
            return content.strip() or None
        # pre-normalised shape used by tests
        text = rec.get("text")
        if isinstance(text, str):
            return text.strip() or None
        return None
    else:
        if getattr(rec, "type", None) != "user":
            return None
        text = getattr(rec, "text", None)
        if isinstance(text, str):
            return text.strip() or None
        return None


def _is_subagent_turn(rec: Any) -> bool:
    """Return True if this assistant record uses a TaskCreate-style tool."""
    if isinstance(rec, dict):
        if rec.get("type") != "assistant":
            return False
        msg = rec.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else []
    else:
        if getattr(rec, "type", None) != "assistant":
            return False
        msg = getattr(rec, "message", None) or {}
        content = msg.get("content") if isinstance(msg, dict) else []
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name") or ""
            if name in _SUBAGENT_TOOL_NAMES:
                return True
    return False


def _parse_hour(ts: str | None) -> int | None:
    """Parse ISO8601 timestamp → UTC hour (0–23), or None on failure."""
    if not ts:
        return None
    try:
        # Handle both "2025-05-01T14:32:00.000Z" and "2025-05-01T14:32:00+00:00"
        ts_clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_clean)
        return dt.astimezone(timezone.utc).hour
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Work-window classifier
# ---------------------------------------------------------------------------


def _work_window(hour_counts: Counter) -> str:
    """Map an hour-of-day Counter to a named work-window label.

    Bins:
      morning    06–11
      afternoon  12–17
      evening    18–21
      late_night 22–05
    """
    if not hour_counts:
        return "mixed"

    total = sum(hour_counts.values())
    if total == 0:
        return "mixed"

    bins = {
        "morning":    sum(hour_counts[h] for h in range(6, 12)),
        "afternoon":  sum(hour_counts[h] for h in range(12, 18)),
        "evening":    sum(hour_counts[h] for h in range(18, 22)),
        "late_night": sum(hour_counts[h] for h in list(range(22, 24)) + list(range(0, 6))),
    }
    dominant_bin, dominant_count = max(bins.items(), key=lambda x: x[1])
    # Only assign a specific label if that bin has majority (>50 %) of activity.
    if dominant_count / total >= 0.50:
        return dominant_bin
    return "mixed"


# ---------------------------------------------------------------------------
# Session-shape classifier
# ---------------------------------------------------------------------------


def _session_shape(msg_counts: list[int]) -> str:
    """Label the distribution of user-messages-per-session.

    ops_burst  — majority of sessions are short (<= _OPS_BURST_MAX msgs)
    marathon   — majority are long (>= _MARATHON_MIN msgs)
    focused    — middle range dominates (11–199 msgs)
    bimodal    — both ops_burst and marathon are each >= 20 % of sessions
    mixed      — no clear pattern
    """
    if not msg_counts:
        return "mixed"
    n = len(msg_counts)
    ops = sum(1 for c in msg_counts if c <= _OPS_BURST_MAX)
    marathon = sum(1 for c in msg_counts if c >= _MARATHON_MIN)
    ops_frac = ops / n
    marathon_frac = marathon / n

    if ops_frac >= 0.50:
        return "ops_burst"
    if marathon_frac >= 0.50:
        return "marathon"
    if ops_frac >= 0.20 and marathon_frac >= 0.20:
        return "bimodal"
    # >50 % middle-range
    middle = n - ops - marathon
    if middle / n >= 0.50:
        return "focused"
    return "mixed"


# ---------------------------------------------------------------------------
# Full-corpus extraction
# ---------------------------------------------------------------------------


def extract_workflow(jsonl_paths: Iterable[Path]) -> WorkflowProfile:
    """Scan all JSONL session files and return a :class:`WorkflowProfile`.

    Tolerant of malformed records — individual bad JSON lines are skipped.
    Empty corpus → all-zero / empty-string defaults (stable shape).
    """
    sessions: dict[str, dict] = {}
    hour_counter: Counter = Counter()
    fan_out_vocab: Counter = Counter()
    agent_counts: list[int] = []
    planning_idiom_votes: Counter = Counter()

    for path in jsonl_paths:
        try:
            _process_file(
                Path(path),
                sessions,
                hour_counter,
                fan_out_vocab,
                agent_counts,
                planning_idiom_votes,
            )
        except Exception as exc:
            log.debug("skipping %s: %s", path, exc)
            continue

    return _build_profile(sessions, hour_counter, fan_out_vocab, agent_counts, planning_idiom_votes)


def _process_record(
    rec: dict,
    fallback_sid: str,
    sessions: dict,
    hour_counter: Counter,
    fan_out_vocab: Counter,
    agent_counts: list,
    planning_idiom_votes: Counter,
) -> None:
    """Update all accumulators for a single already-parsed record dict."""
    if not isinstance(rec, dict):
        return

    sid = _record_session_id(rec) or fallback_sid
    if sid not in sessions:
        sessions[sid] = {
            "fan_out_hits": 0,
            "user_turns": 0,
            "autonomy_hits": 0,
            "interrupts": 0,
            "has_subagent": False,
        }
    sess = sessions[sid]

    rec_type = _record_type(rec)

    # Track message timestamps for hour distribution.
    ts = _record_timestamp(rec)
    hour = _parse_hour(ts)
    if hour is not None:
        hour_counter[hour] += 1

    # User turns: fan-out, autonomy, planning idiom.
    if rec_type == "user":
        text = _user_text(rec)
        if text:
            sess["user_turns"] += 1
            text_lc = text.lower()

            # Fan-out vocabulary detection.
            for phrase, pattern in _FAN_OUT_PATTERNS:
                if pattern.search(text):
                    sess["fan_out_hits"] += 1
                    fan_out_vocab[phrase] += 1
                    # Also capture the agent count when present.
                    m = _AGENT_COUNT_RE.search(text)
                    if m:
                        try:
                            agent_counts.append(int(m.group(1)))
                        except ValueError:
                            pass
                    break  # count once per turn

            # Autonomy / fire-and-forget detection.
            tokens = text_lc.split()
            if len(tokens) == 1 and tokens[0] in _AUTONOMY_WORDS:
                sess["autonomy_hits"] += 1
            else:
                for ap in _AUTONOMY_PHRASES:
                    if text_lc.strip() == ap:
                        sess["autonomy_hits"] += 1
                        break

            # Planning idiom detection (first idiom found per turn counts).
            for idiom, pattern in _PLANNING_IDIOMS:
                if pattern.search(text):
                    planning_idiom_votes[idiom] += 1
                    break

    # Interrupt (queued command mid-flight).
    elif rec_type in _INTERRUPT_TYPES:
        sess["interrupts"] += 1

    # Subagent tool use.
    elif rec_type == "assistant":
        if _is_subagent_turn(rec):
            sess["has_subagent"] = True


def _process_file(
    path: Path,
    sessions: dict,
    hour_counter: Counter,
    fan_out_vocab: Counter,
    agent_counts: list,
    planning_idiom_votes: Counter,
) -> None:
    """Stream one JSONL file and update all accumulators in-place."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                rec = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            _process_record(
                rec, str(path),
                sessions, hour_counter, fan_out_vocab, agent_counts,
                planning_idiom_votes,
            )


def _median_int(values: list[int]) -> float:
    """Return the median of a list of ints (sorted index, nearest-rank)."""
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return float(s[mid])


# ---------------------------------------------------------------------------
# Incremental EMA-blended update (Stop-hook hot path)
# ---------------------------------------------------------------------------

# EMA window: a batch of 50 sessions has full weight; smaller batches drift
# existing values gently.  50 is large enough to not thrash on single sessions
# yet small enough to converge in weeks of typical use.
_EMA_WINDOW = 50

# Float fields updated via EMA.
_EMA_FLOAT_FIELDS: tuple[str, ...] = (
    "fan_out_hits_per_session",
    "autonomy_score",
    "interrupt_rate",
    "subagent_adoption",
)

# Int fields updated via EMA (rounded after blend).
_EMA_INT_FIELDS: tuple[str, ...] = ("typical_agent_count",)


def _extract_workflow_from_records(records: Iterable[dict]) -> WorkflowProfile:
    """Build a :class:`WorkflowProfile` from an iterable of pre-parsed record dicts.

    This is the record-stream core shared by :func:`extract_workflow_incremental`
    (hot path, records already in memory) and the full-corpus path (which reads
    from JSONL files via :func:`_process_file`).

    Records are the same raw Claude-Code-shaped dicts that
    ``_profile_record_snapshot`` emits and that voice/operator incrementals
    consume.  Extra keys are silently ignored; missing keys fall back to defaults.
    """
    sessions: dict[str, dict] = {}
    hour_counter: Counter = Counter()
    fan_out_vocab: Counter = Counter()
    agent_counts: list[int] = []
    planning_idiom_votes: Counter = Counter()

    for rec in records:
        if not isinstance(rec, dict):
            continue
        _process_record(
            rec, "_records_batch",
            sessions, hour_counter, fan_out_vocab, agent_counts,
            planning_idiom_votes,
        )

    return _build_profile(
        sessions, hour_counter, fan_out_vocab, agent_counts, planning_idiom_votes
    )


def _build_profile(
    sessions: dict,
    hour_counter: Counter,
    fan_out_vocab: Counter,
    agent_counts: list[int],
    planning_idiom_votes: Counter,
) -> WorkflowProfile:
    """Aggregate per-session accumulators into a :class:`WorkflowProfile`."""
    session_list = list(sessions.values())
    sample_size = len(session_list)

    if sample_size == 0:
        return WorkflowProfile()

    total_fan_out = sum(s["fan_out_hits"] for s in session_list)
    fan_out_hits_per_session = total_fan_out / sample_size
    fan_out_vocabulary = [term for term, _ in fan_out_vocab.most_common(10)]
    typical_agent_count = int(_median_int(agent_counts)) if agent_counts else 0
    total_user = sum(s["user_turns"] for s in session_list)
    total_auto = sum(s["autonomy_hits"] for s in session_list)
    autonomy_score = (total_auto / total_user) if total_user > 0 else 0.0
    total_interrupts = sum(s["interrupts"] for s in session_list)
    interrupt_rate = total_interrupts / sample_size
    planning_idiom = planning_idiom_votes.most_common(1)[0][0] if planning_idiom_votes else ""
    peak_hours = [h for h, _ in hour_counter.most_common(3)]
    preferred_work_window = _work_window(hour_counter)
    msg_counts = [s["user_turns"] for s in session_list]
    session_shape = _session_shape(msg_counts)
    sessions_with_sub = sum(1 for s in session_list if s["has_subagent"])
    subagent_adoption = sessions_with_sub / sample_size

    return WorkflowProfile(
        fan_out_hits_per_session=round(fan_out_hits_per_session, 4),
        fan_out_vocabulary=fan_out_vocabulary,
        typical_agent_count=typical_agent_count,
        autonomy_score=round(min(1.0, max(0.0, autonomy_score)), 4),
        interrupt_rate=round(interrupt_rate, 4),
        planning_idiom=planning_idiom,
        peak_hours=peak_hours,
        preferred_work_window=preferred_work_window,
        session_shape=session_shape,
        subagent_adoption=round(subagent_adoption, 4),
        sample_size=sample_size,
    )


def _normalize_existing(
    existing: "WorkflowProfile | dict | None",
) -> WorkflowProfile:
    """Coerce existing to a :class:`WorkflowProfile`, stripping sidecar keys.

    Accepts:
    * ``None`` / empty dict → all-zero defaults (cold-start).
    * dict (from ``index.workflow.get_workflow()``) — strip ``_updated_ts``
      and any other ``_``-prefixed sidecar keys, map remaining fields.
    * :class:`WorkflowProfile` — returned unchanged.
    """
    if existing is None:
        return WorkflowProfile()
    if isinstance(existing, WorkflowProfile):
        return existing
    # dict path — strip sidecars, coerce field by field.
    profile = WorkflowProfile()
    for key, val in existing.items():
        if key.startswith("_"):
            continue
        if hasattr(profile, key):
            try:
                setattr(profile, key, val)
            except Exception:  # noqa: BLE001
                pass
    return profile


def extract_workflow_incremental(
    records: Iterable[dict],
    existing: "WorkflowProfile | dict | None" = None,
    window_size: int = _EMA_WINDOW,
) -> WorkflowProfile:
    """Update the workflow profile via EMA over a rolling window.

    Signature change (v0.9.1 fix): the first argument is now an iterable of
    **pre-parsed record dicts** (the same shape ``_profile_record_snapshot``
    emits), NOT a list of file paths.  This matches what ``_commit_parsed``
    in ``index/ingest.py`` actually passes on the Stop-hook hot path.

    ``existing`` is dict-tolerant: it accepts a raw dict as returned by
    ``index.workflow.get_workflow()`` (including the ``_updated_ts`` sidecar),
    a :class:`WorkflowProfile` object (back-compat), or ``None`` (cold start).

    The EMA weight is ``alpha = min(1.0, batch_sessions / window_size)``.
    An absent existing profile (or a batch larger than the window) snaps
    directly to the batch values — so cold-start is identical to a full-corpus
    run over the same records.

    List/string fields (fan_out_vocabulary, planning_idiom, peak_hours,
    preferred_work_window, session_shape) are taken from the batch when the
    batch has > 0 sessions, otherwise inherited from existing — they encode
    *current* observed patterns better than a weighted average.
    """
    batch = _extract_workflow_from_records(records)
    batch_n = batch.sample_size

    existing_wp = _normalize_existing(existing)

    if existing_wp.sample_size == 0:
        return batch
    if batch_n == 0:
        return existing_wp

    alpha = min(1.0, batch_n / float(window_size))

    def _blend_float(old: float, new: float) -> float:
        return round(alpha * new + (1.0 - alpha) * old, 4)

    def _blend_int(old: int, new: int) -> int:
        return int(round(alpha * new + (1.0 - alpha) * old))

    # Blend numeric fields.
    fohps = _blend_float(existing_wp.fan_out_hits_per_session, batch.fan_out_hits_per_session)
    auto = min(1.0, max(0.0, _blend_float(existing_wp.autonomy_score, batch.autonomy_score)))
    ir = _blend_float(existing_wp.interrupt_rate, batch.interrupt_rate)
    sub = min(1.0, max(0.0, _blend_float(existing_wp.subagent_adoption, batch.subagent_adoption)))
    tac = _blend_int(existing_wp.typical_agent_count, batch.typical_agent_count)

    # For categorical / list fields, prefer the batch (most-recent signal),
    # falling back to existing when the batch gives nothing.
    vocab = batch.fan_out_vocabulary if batch.fan_out_vocabulary else existing_wp.fan_out_vocabulary
    idiom = batch.planning_idiom if batch.planning_idiom else existing_wp.planning_idiom
    peak = batch.peak_hours if batch.peak_hours else existing_wp.peak_hours
    window = batch.preferred_work_window if batch_n > 0 else existing_wp.preferred_work_window
    shape = batch.session_shape if batch_n > 0 else existing_wp.session_shape

    prior_n = existing_wp.sample_size
    new_n = min(window_size, prior_n + batch_n)

    return WorkflowProfile(
        fan_out_hits_per_session=fohps,
        fan_out_vocabulary=vocab,
        typical_agent_count=tac,
        autonomy_score=auto,
        interrupt_rate=ir,
        planning_idiom=idiom,
        peak_hours=peak,
        preferred_work_window=window,
        session_shape=shape,
        subagent_adoption=sub,
        sample_size=new_n,
    )
