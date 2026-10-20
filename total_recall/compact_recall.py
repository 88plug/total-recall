"""Post-compaction recall text.

The harness summary is lossy. This block is the memory it does not keep:
who the operator is, what is banned, what was decided, what was corrected,
and a few indexed facts for the work in flight. Callers inject it on the
channel that reaches the model after compaction. It stands alone when
amnesia is not installed. Empty string means "nothing to inject".
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _topic_from_packet(db_path: Path, session_id: str | None, explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()[:400]
    if not session_id:
        return ""
    packet_path = db_path.parent / "sessions" / f"{session_id}.continuation.json"
    if not packet_path.is_file():
        return ""
    try:
        data = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("last_user_directive", "active_goal", "next_step"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:400]
        if isinstance(value, dict):
            for inner in ("goal", "text", "title"):
                piece = value.get(inner)
                if isinstance(piece, str) and piece.strip():
                    return piece.strip()[:400]
    return ""


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _operator_lines(cwd: str | None) -> list[str]:
    """Durable memory the compaction summary does not keep."""
    try:
        from mcp_server.extras.operator_context_tools import get_operator_context
    except Exception:
        return []
    try:
        payload = get_operator_context(cwd=cwd, max_chars=4000)
    except Exception:
        return []
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    lines: list[str] = []
    ident = payload.get("identity")
    if isinstance(ident, dict):
        name = str(ident.get("name") or "").strip()
        handle = str(ident.get("handle") or "").strip()
        who = " ".join(bit for bit in (name, f"@{handle}" if handle else "") if bit)
        if who:
            lines.append(f"Operator: {who}")
    goal = payload.get("active_goal")
    goal_text = ""
    if isinstance(goal, dict):
        goal_text = str(goal.get("goal") or "").strip()
    elif isinstance(goal, str):
        goal_text = goal.strip()
    if goal_text:
        lines.append("Active goal: " + _clip(goal_text, 240))
    bans = payload.get("bans")
    if isinstance(bans, list):
        for ban in bans[:4]:
            if not isinstance(ban, dict):
                continue
            target = str(ban.get("target") or "").strip()
            if not target:
                continue
            reason = str(ban.get("reason") or "").strip()
            line = f"Ban: {target}"
            if reason:
                line += " — " + _clip(reason, 140)
            lines.append(line)
    decisions = payload.get("standing_decisions")
    if isinstance(decisions, list):
        for dec in decisions[:4]:
            if not isinstance(dec, dict):
                continue
            topic = str(dec.get("topic") or "").strip()
            chose = str(dec.get("chose") or "").strip()
            if not topic or not chose:
                continue
            line = f"Decision: {topic} = {chose}"
            over = str(dec.get("over") or "").strip()
            if over:
                line += f", not {over}"
            lines.append(line)
    corrections = payload.get("recent_corrections")
    if isinstance(corrections, list):
        for cor in corrections[:2]:
            text = ""
            if isinstance(cor, dict):
                text = str(cor.get("correction") or "").strip()
            elif isinstance(cor, str):
                text = cor.strip()
            if text:
                lines.append("Correction: " + _clip(text, 200))
    return lines


def _hit_lines(conn: sqlite3.Connection, cwd: str | None, topic: str) -> list[str]:
    try:
        from index.query import search_extractions
    except Exception:
        return []

    def _run(query: str | None) -> list[Any]:
        return search_extractions(conn, query=query, cwd=cwd, limit=3)

    hits: list[Any] = []
    if topic:
        try:
            hits = _run(topic)
        except Exception:
            hits = []
    if not hits:
        try:
            hits = _run(None)
        except Exception:
            hits = []
    lines: list[str] = []
    for hit in hits[:3]:
        kind = str(getattr(hit, "kind", "") or "memory")
        content = str(getattr(hit, "content", "") or "").strip().replace("\n", " ")
        if not content:
            continue
        lines.append(f"- {kind}: {content[:240]}")
    return lines


def render_compact_recall(
    *,
    db_path: Path,
    cwd: str | None = None,
    session_id: str | None = None,
    topic: str | None = None,
    max_chars: int = 2500,
) -> str:
    """Return the post-compact recall block, or ``""`` when there is nothing.

    Does not create a database. A missing or unreadable index is silence.
    """
    path = Path(db_path)
    if not path.is_file():
        return ""
    topic_text = _topic_from_packet(path, session_id, topic)
    parts = _operator_lines(cwd)
    seen = {line.split(": ", 1)[-1].casefold() for line in parts}
    try:
        from index.db import connect

        conn = connect(path)
    except Exception:
        conn = None
    if conn is not None:
        try:
            for line in _hit_lines(conn, cwd, topic_text):
                content = line.split(": ", 1)[-1].casefold()
                if any(content and (content in prior or prior in content) for prior in seen):
                    continue
                parts.append(line)
                seen.add(content)
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    if not parts:
        return ""
    body = "\n".join(parts)
    text = (
        "[total-recall] POST-COMPACTION RECALL\n"
        "Still in force after compaction. The summary does not keep this.\n"
        f"{body}\n"
        "[total-recall] END RECALL"
    )
    cap = max(200, int(max_chars))
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text
