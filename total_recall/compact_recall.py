"""Post-compaction recall text.

One short block: operator context plus a few indexed hits. Callers inject it
on the harness channel that actually reaches the model after compaction.
Empty string means "nothing to inject" — never an error payload.
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


def _operator_blob(cwd: str | None) -> str:
    try:
        from mcp_server.extras.operator_context_tools import get_operator_context
    except Exception:
        return ""
    try:
        payload = get_operator_context(cwd=cwd, max_chars=900)
    except Exception:
        return ""
    if not isinstance(payload, dict) or payload.get("error") or not payload:
        return ""
    payload.pop("_kind", None)
    payload.pop("_cwd", None)
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


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
    parts: list[str] = []
    operator = _operator_blob(cwd)
    if operator:
        parts.append(operator)
    try:
        from index.db import connect

        conn = connect(path)
    except Exception:
        conn = None
    if conn is not None:
        try:
            parts.extend(_hit_lines(conn, cwd, topic_text))
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    if not parts:
        return ""
    body = "\n".join(parts)
    text = f"[total-recall] POST-COMPACTION RECALL\n{body}\n[total-recall] END RECALL"
    cap = max(200, int(max_chars))
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text
