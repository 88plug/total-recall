"""Continuation-packet builder for Claude Code compaction recovery.

Claude Code compaction collapses a ~1M-token conversation down to ~10-17k
tokens: only the last handful of messages survive verbatim, plus a synthetic
9-section prose summary (a ``user`` record with ``isCompactSummary: true``
immediately after a ``type=system, subtype=compact_boundary`` record). That
summary is good at *narrative* but routinely drops the concrete in-flight
state the model needs to keep working: which files were open, the exact
command that just failed, the still-pending TODO, the precise next step.

This module builds a small, bounded *continuation packet* that augments the
native summary with that durable + in-flight state. It is a **pure builder**
shared by two callers:

* the live ``PreCompact`` / ``SessionStart`` hook (whole-file mode), and
* the backtest harness (``scripts/backtest_compaction.py``), which replays a
  *real* historical boundary by passing ``boundary_idx`` so the packet is
  built from exactly the records the model had *before* the compaction — no
  post-boundary leakage.

Two provenance lanes feed the packet:

* **Tail-derived** (always): parsed from the transcript JSONL itself. The
  index carries no ``tool_use`` payloads (``raw_json`` is NULL), so in-flight
  state — open files, last actions, the pending plan — must come from the
  live transcript tail.
* **Index-derived** (only when ``db_path`` is given): durable cross-session
  state — the active goal, standing decisions, recent model corrections,
  failed attempts. Always time-guarded to ``ts < boundary_ts`` when a
  boundary is supplied, so a replay can never see the future.

Design contract:

* **Pure stdlib + sqlite3.** No third-party imports. ``index.goals`` /
  ``index.paths`` are imported *defensively* (best-effort durable goal); if
  they are absent the field is simply omitted.
* **Never raises.** Every field is collected under its own ``try/except``;
  any failure omits that one field and the rest of the packet still ships.
* **Hard budget.** ``max_chars`` caps the JSON-serialized packet. Fields are
  evicted from the *tail* of a fixed priority order and then truncated to
  fit, mirroring ``operator_context_tools._build_payload``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Operate on at most this many trailing records of the pre-boundary window.
# A compaction window can hold ~1M tokens / thousands of records; the
# in-flight signal lives in the last stretch of work, and scanning the whole
# file for every boundary in a backtest is wasteful.
_TAIL_RECORDS = 400

# Tool names whose inputs name a file we might still be working on.
_FILE_TOOLS = {"Edit", "Write", "Read", "NotebookEdit", "MultiEdit"}
_FILE_INPUT_KEYS = ("file_path", "path", "notebook_path")

# Priority order for the budget. Highest-value fields appear first and are the
# *last* to be evicted when ``max_chars`` is tight. The durable goal grounds
# *why* we're here; the last user directive grounds *what* was asked; the
# files + last actions ground *where* we left off.
_PRIORITY: tuple[str, ...] = (
    "active_goal",
    "last_user_directive",
    "files_in_flight",
    "last_actions",
    "decisions_this_session",
    "open_plan",
    "next_step",
    "failed_attempts_this_session",
)

_PLAN_RE = re.compile(r"(?i)(next:|plan:|todo:|remaining)")
# Sentence splitter for next_step: split on ., !, ? followed by whitespace.
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------
# JSONL parsing helpers
# ---------------------------------------------------------------------------


def _read_records(transcript_path: str | Path, boundary_idx: int | None) -> list[dict]:
    """Return parsed JSONL records in ``[0, boundary_idx)`` (or whole file).

    Blank/garbage lines are skipped. ``boundary_idx`` counts *physical lines*
    of the file (matching how the backtest enumerates boundaries), so we cut
    the raw line list before parsing.
    """
    out: list[dict] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if boundary_idx is not None and i >= boundary_idx:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _boundary_ts(transcript_path: str | Path, boundary_idx: int | None) -> int | None:
    """Epoch-seconds of the boundary record's ``timestamp``, or None.

    Used to time-guard index queries so a replay never reads extractions that
    were derived from *after* the compaction point.
    """
    if boundary_idx is None:
        return None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i < boundary_idx:
                    continue
                if i > boundary_idx:
                    break
                rec = json.loads(line)
                return _iso_to_epoch(rec.get("timestamp"))
    except (OSError, ValueError, TypeError):
        return None
    return None


def _iso_to_epoch(ts: Any) -> int | None:
    """Best-effort ISO8601 (or numeric) timestamp → epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    # All-digit string → already epoch.
    if s.isdigit():
        return int(s)
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


# Synthetic "user" envelopes injected by the harness (task notifications,
# slash-command echoes, local-command output, system reminders). These are
# not genuine human directives; mirrors extractors.base._SKIP_PREFIXES.
_SKIP_USER_PREFIXES = (
    "<task-notification>",
    "<command-message>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
)


def _msg(rec: dict) -> dict:
    m = rec.get("message")
    return m if isinstance(m, dict) else {}


def _content_blocks(rec: dict) -> list:
    """Normalize ``message.content`` to a list of blocks.

    A plain string content becomes a single synthetic ``text`` block so
    callers can treat both shapes uniformly.
    """
    c = _msg(rec).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        return c
    return []


def _text_of(rec: dict) -> str:
    """Concatenate the ``text`` blocks of a record's message content."""
    parts = []
    for b in _content_blocks(rec):
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
    return "\n".join(parts)


def _is_meta(rec: dict) -> bool:
    return bool(rec.get("isMeta") or rec.get("isCompactSummary"))


def _is_real_user(rec: dict) -> bool:
    """A genuine user *text* turn (not meta, not a tool_result envelope)."""
    if rec.get("type") != "user":
        return False
    if _is_meta(rec):
        return False
    blocks = _content_blocks(rec)
    if not blocks:
        return False
    has_text = any(
        isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
        for b in blocks
    )
    only_tool_result = blocks and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
    )
    if not (has_text and not only_tool_result):
        return False
    # Reject synthetic envelopes (task notifications, slash-command echoes,
    # local-command output) that masquerade as user text turns.
    stripped = _text_of(rec).lstrip()
    return not any(stripped.startswith(pfx) for pfx in _SKIP_USER_PREFIXES)


# ---------------------------------------------------------------------------
# Tail-derived collectors
# ---------------------------------------------------------------------------


def _last_user_directive(tail: list[dict]) -> str | None:
    """Last genuine user text record in the window."""
    for rec in reversed(tail):
        if _is_real_user(rec):
            txt = _text_of(rec)
            if txt:
                return txt
    return None


def _iter_tool_uses(rec: dict):
    for b in _content_blocks(rec):
        if isinstance(b, dict) and b.get("type") == "tool_use":
            yield b


def _files_in_flight(tail: list[dict]) -> list[dict]:
    """Top-5 dedup file targets, most-recent-first, with verb + count."""
    # Walk newest→oldest so "most recent" ordering falls out naturally; track
    # first-seen verb (the most recent action on that file) and total count.
    order: list[str] = []
    info: dict[str, dict] = {}
    for rec in reversed(tail):
        for tu in _iter_tool_uses(rec):
            name = tu.get("name")
            if name not in _FILE_TOOLS:
                continue
            inp = tu.get("input") or {}
            if not isinstance(inp, dict):
                continue
            path = None
            for k in _FILE_INPUT_KEYS:
                v = inp.get(k)
                if isinstance(v, str) and v.strip():
                    path = v.strip()
                    break
            if not path:
                continue
            if path not in info:
                order.append(path)
                info[path] = {"path": path, "verb": name, "count": 0}
            info[path]["count"] += 1
    return [info[p] for p in order[:5]]


def _pair_tool_results(tail: list[dict]) -> dict[str, dict]:
    """Map ``tool_use_id`` → ``{is_error, content}`` from tool_result blocks."""
    results: dict[str, dict] = {}
    for rec in tail:
        for b in _content_blocks(rec):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    results[tid] = b
    return results


def _result_ok(result: dict | None) -> bool:
    """True when a paired tool_result indicates success."""
    if result is None:
        return True  # no result captured (in-flight / cut off) → assume ok
    if result.get("is_error"):
        return False
    content = result.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    low = text.lower()
    return not ("error" in low or "traceback" in low or "command failed" in low)


def _tool_arg(tu: dict) -> str | None:
    """Compact argument for a tool_use: Bash command head, else file path."""
    name = tu.get("name")
    inp = tu.get("input") or {}
    if not isinstance(inp, dict):
        return None
    if name == "Bash":
        cmd = inp.get("command")
        if isinstance(cmd, str) and cmd.strip():
            cmd = cmd.strip()
            return cmd[:80]
    for k in _FILE_INPUT_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Fall back to a query-ish field if present (Grep/Glob/etc.).
    for k in ("pattern", "query", "url", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return None


def _last_actions(tail: list[dict]) -> list[dict]:
    """Last 3 tool_use→result pairs, most-recent-first."""
    results = _pair_tool_results(tail)
    actions: list[dict] = []
    for rec in reversed(tail):
        # Within a record, iterate tool_uses in reverse so the newest comes
        # first across the flattened list.
        for tu in reversed(list(_iter_tool_uses(rec))):
            arg = _tool_arg(tu)
            tid = tu.get("id")
            actions.append(
                {
                    "tool": tu.get("name"),
                    "arg": arg,
                    "ok": _result_ok(results.get(tid) if isinstance(tid, str) else None),
                }
            )
            if len(actions) >= 3:
                return actions
    return actions


def _open_plan(tail: list[dict]) -> Any:
    """Most recent TodoWrite input, else last assistant plan-ish text block."""
    for rec in reversed(tail):
        for tu in _iter_tool_uses(rec):
            if tu.get("name") == "TodoWrite":
                inp = tu.get("input")
                if isinstance(inp, dict):
                    todos = inp.get("todos")
                    if isinstance(todos, list) and todos:
                        return todos
                    return inp
    # Fallback: last assistant text block matching the plan regex.
    for rec in reversed(tail):
        if _msg(rec).get("role") != "assistant":
            continue
        txt = _text_of(rec)
        if txt and _PLAN_RE.search(txt):
            return txt
    return None


def _next_step(tail: list[dict]) -> str | None:
    """Final actionable sentence of the last assistant text block."""
    for rec in reversed(tail):
        if _msg(rec).get("role") != "assistant":
            continue
        txt = _text_of(rec)
        if not txt:
            continue
        sentences = [s.strip() for s in _SENT_RE.split(txt) if s.strip()]
        if sentences:
            return sentences[-1]
    return None


# ---------------------------------------------------------------------------
# Index-derived collectors (time-guarded, read-only)
# ---------------------------------------------------------------------------


def _ro_connect(db_path: str) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        log.debug("continuation_packet: ro connect failed: %s", e)
        return None


def _project_key(cwd: str | None) -> str | None:
    """Worktree→repo pooling via index.paths, with a no-op fallback."""
    try:
        from index.paths import project_key  # type: ignore

        return project_key(cwd)
    except Exception:  # noqa: BLE001
        return cwd


def _active_goal(conn: sqlite3.Connection, cwd: str | None, boundary_ts: int | None) -> str | None:
    """Durable active goal text for this project, time-guarded.

    Prefers ``index.goals.get_active_goal``; falls back to the first line of
    the most recent ``away_summary`` extraction for this project.
    """
    pkey = _project_key(cwd)
    # Primary: goal_stack via the goals API.
    try:
        from index.goals import get_active_goal  # type: ignore

        goal = None if pkey is None else get_active_goal(conn, pkey)
        if goal is not None:
            text = getattr(goal, "goal_text", None)
            declared = getattr(goal, "declared_ts", None)
            last_prog = getattr(goal, "last_progress_ts", None)
            gts = last_prog or declared
            if text and (boundary_ts is None or gts is None or int(gts) < boundary_ts):
                return str(text)
    except Exception as e:  # noqa: BLE001
        log.debug("continuation_packet: get_active_goal failed: %s", e)

    # Fallback: most recent pre-boundary away_summary for this project.
    try:
        row = _query_away_summary(conn, cwd, pkey, boundary_ts)
        if row:
            first_line = row.split("\n", 1)[0].strip()
            if first_line:
                return first_line
    except Exception as e:  # noqa: BLE001
        log.debug("continuation_packet: away_summary fallback failed: %s", e)
    return None


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)
    except sqlite3.Error:
        return False


def _query_away_summary(
    conn: sqlite3.Connection, cwd: str | None, pkey: str | None, boundary_ts: int | None
) -> str | None:
    """Most recent away_summary content for this project, time-guarded."""
    clauses = ["kind = 'away_summary'"]
    params: list[Any] = []
    # Scope by project: prefer project_key column when present, else cwd.
    if _has_column(conn, "extractions", "project_key") and pkey:
        clauses.append("(project_key = ? OR cwd = ?)")
        params += [pkey, cwd]
    elif cwd:
        clauses.append("cwd = ?")
        params.append(cwd)
    if boundary_ts is not None:
        clauses.append("ts IS NOT NULL AND ts < ?")
        params.append(boundary_ts)
    sql = (
        "SELECT content FROM extractions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY ts DESC LIMIT 1"
    )
    row = conn.execute(sql, params).fetchone()
    return row["content"] if row else None


def _extractions_by_kind_session(
    conn: sqlite3.Connection,
    kinds: tuple[str, ...],
    session_id: str | None,
    boundary_ts: int | None,
    limit: int,
) -> list[str]:
    """Top-N extraction ``content`` for the given kinds + session, guarded."""
    if not session_id:
        return []
    placeholders = ",".join("?" for _ in kinds)
    clauses = [f"kind IN ({placeholders})", "session_id = ?"]
    params: list[Any] = list(kinds) + [session_id]
    if boundary_ts is not None:
        clauses.append("(ts IS NULL OR ts < ?)")
        params.append(boundary_ts)
    sql = (
        "SELECT content FROM extractions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY score DESC, ts DESC LIMIT ?"
    )
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        log.debug("continuation_packet: extractions query failed: %s", e)
        return []
    return [r["content"] for r in rows if r["content"]]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def _serialize(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _truncate(val: Any, limit: int) -> Any:
    """Truncate a string (or list of strings) field to fit a char budget."""
    if isinstance(val, str):
        if len(val) > limit:
            return val[: max(40, limit - 1)] + "…"
        return val
    if isinstance(val, list):
        out = []
        used = 0
        for item in val:
            if isinstance(item, str):
                s = item
            elif isinstance(item, dict):
                s = _serialize(item)
            else:
                s = str(item)
            if used + len(s) > limit and out:
                break
            out.append(item)
            used += len(s)
        return out
    return val


def _apply_budget(fields: dict, max_chars: int) -> dict:
    """Order by priority, evict from the tail, then truncate to fit."""
    pruned: dict = {}
    for key in _PRIORITY:
        val = fields.get(key)
        if val in (None, "", [], {}):
            continue
        pruned[key] = val

    if len(_serialize(pruned)) <= max_chars:
        return pruned

    # Evict whole fields from the tail of the priority order, but always keep
    # at least the single highest-priority field (it gets truncated below).
    keys_in_order = [k for k in _PRIORITY if k in pruned]
    while len(keys_in_order) > 1 and len(_serialize(pruned)) > max_chars:
        victim = keys_in_order.pop()
        pruned.pop(victim, None)

    # Still over (a single high-priority field is huge) → truncate fields.
    if len(_serialize(pruned)) > max_chars:
        per_field = max(80, max_chars // max(1, len(pruned)))
        for k in list(pruned):
            pruned[k] = _truncate(pruned[k], per_field)

    # Pathological last resort: keep only the top field, shrinking its budget
    # until the serialized packet fits (the JSON wrapper + an escaped ellipsis
    # cost a few extra chars, so loop rather than guess a fixed margin).
    if len(_serialize(pruned)) > max_chars and pruned:
        first = next(iter(pruned))
        original = pruned[first]
        budget = max_chars - len(first) - 10
        while budget > 0:
            pruned = {first: _truncate(original, budget)}
            if len(_serialize(pruned)) <= max_chars:
                break
            budget -= 16

    return pruned


# ---------------------------------------------------------------------------
# Compact rendering (shared by the SessionStart restore hook + the
# UserPromptSubmit bridge). Turns the packet dict into a short human-readable
# block, hard-capped so it stays well under the SessionStart 10k additionalContext
# limit. Pure stdlib, never raises.
# ---------------------------------------------------------------------------


def _render_one(key: str, val: Any) -> list[str]:
    """Render a single packet field to a list of lines. Best-effort."""
    label = {
        "active_goal": "Active goal",
        "last_user_directive": "Last directive",
        "files_in_flight": "Files in flight",
        "last_actions": "Last actions",
        "decisions_this_session": "Decisions this session",
        "open_plan": "Open plan",
        "next_step": "Next step",
        "failed_attempts_this_session": "Failed attempts this session",
    }.get(key, key)

    lines: list[str] = []
    if key == "files_in_flight" and isinstance(val, list):
        lines.append(f"{label}:")
        for f in val[:6]:
            if isinstance(f, dict):
                path = f.get("path", "")
                verb = f.get("verb", "")
                lines.append(f"  - {verb} {path}".rstrip())
            else:
                lines.append(f"  - {f}")
    elif key == "last_actions" and isinstance(val, list):
        lines.append(f"{label}:")
        for a in val[:5]:
            if isinstance(a, dict):
                tool = a.get("tool", "")
                arg = str(a.get("arg", "")).replace("\n", " ")[:120]
                ok = "ok" if a.get("ok") else "FAILED"
                lines.append(f"  - [{ok}] {tool}: {arg}".rstrip())
            else:
                lines.append(f"  - {a}")
    elif isinstance(val, list):
        lines.append(f"{label}:")
        for item in val[:5]:
            if isinstance(item, dict):
                txt = item.get("text") or item.get("goal") or json.dumps(item, ensure_ascii=False)
                lines.append(f"  - {str(txt)[:240]}")
            else:
                lines.append(f"  - {str(item)[:240]}")
    elif isinstance(val, dict):
        txt = val.get("text") or val.get("goal") or json.dumps(val, ensure_ascii=False)
        lines.append(f"{label}: {str(txt)[:280]}")
    else:
        lines.append(f"{label}: {str(val)[:280]}")
    return lines


def render_continuation_packet(packet: dict, max_chars: int = 6000) -> str:
    """Render a packet dict to a compact human-readable block.

    Fields are emitted in ``_PRIORITY`` order (highest value first) so that a
    tight ``max_chars`` evicts the least useful tail first. Returns "" when the
    packet carries no renderable fields. Never raises.
    """
    if not isinstance(packet, dict):
        return ""
    out: list[str] = []
    for key in _PRIORITY:
        if key not in packet:
            continue
        val = packet[key]
        if val in (None, "", [], {}):
            continue
        try:
            block = _render_one(key, val)
        except Exception:  # noqa: BLE001
            continue
        candidate = "\n".join(out + block)
        if len(candidate) > max_chars:
            break
        out.extend(block)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_continuation_packet(
    transcript_path: str | Path,
    session_id: str | None,
    cwd: str | None,
    db_path: str | None = None,
    boundary_idx: int | None = None,
    max_chars: int = 2000,
) -> dict:
    """Build a bounded continuation packet for compaction recovery.

    Parameters
    ----------
    transcript_path:
        Path to the session ``.jsonl``.
    session_id, cwd:
        Identifiers for index scoping (durable lane).
    db_path:
        Read-only index path. When ``None`` the index-derived lane is skipped
        entirely and only tail-derived fields ship.
    boundary_idx:
        Physical-line index of a ``compact_boundary`` record. When given, only
        records ``[0, boundary_idx)`` are read (backtest replay) and all
        index queries are time-guarded to ``ts < boundary_ts``.
    max_chars:
        Hard cap on the JSON-serialized packet.

    Returns a dict of present fields plus a ``_kind`` tag. Never raises:
    any field that fails to collect is simply omitted.
    """
    fields: dict[str, Any] = {}

    # --- Tail-derived lane ---------------------------------------------------
    tail: list[dict] = []
    try:
        records = _read_records(transcript_path, boundary_idx)
        tail = records[-_TAIL_RECORDS:]
    except Exception as e:  # noqa: BLE001
        log.debug("continuation_packet: read failed: %s", e)
        tail = []

    for name, fn in (
        ("last_user_directive", _last_user_directive),
        ("files_in_flight", _files_in_flight),
        ("last_actions", _last_actions),
        ("open_plan", _open_plan),
        ("next_step", _next_step),
    ):
        try:
            val = fn(tail)
            if val not in (None, "", [], {}):
                fields[name] = val
        except Exception as e:  # noqa: BLE001
            log.debug("continuation_packet: %s failed: %s", name, e)

    # --- Index-derived lane (time-guarded, read-only) ------------------------
    if db_path:
        boundary_ts = None
        try:
            boundary_ts = _boundary_ts(transcript_path, boundary_idx)
        except Exception:  # noqa: BLE001
            boundary_ts = None

        conn = _ro_connect(db_path)
        if conn is not None:
            try:
                try:
                    goal = _active_goal(conn, cwd, boundary_ts)
                    if goal:
                        fields["active_goal"] = goal
                except Exception as e:  # noqa: BLE001
                    log.debug("continuation_packet: active_goal failed: %s", e)

                try:
                    decisions = _extractions_by_kind_session(
                        conn,
                        ("standing_decision", "model_correction"),
                        session_id,
                        boundary_ts,
                        limit=3,
                    )
                    if decisions:
                        fields["decisions_this_session"] = decisions
                except Exception as e:  # noqa: BLE001
                    log.debug("continuation_packet: decisions failed: %s", e)

                try:
                    failed = _extractions_by_kind_session(
                        conn, ("failed_attempt",), session_id, boundary_ts, limit=2
                    )
                    if failed:
                        fields["failed_attempts_this_session"] = failed
                except Exception as e:  # noqa: BLE001
                    log.debug("continuation_packet: failed_attempts failed: %s", e)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()

    packet = _apply_budget(fields, max_chars=max(200, int(max_chars)))
    packet["_kind"] = "continuation_packet"
    return packet


__all__ = [
    "build_continuation_packet",
    "render_continuation_packet",
    # Exposed for tests:
    "_apply_budget",
    "_PRIORITY",
    "_read_records",
    "_iso_to_epoch",
]
