"""Pi coding-agent session adapter (earendil-works/pi).

Pi writes append-only JSONL under ``~/.pi/agent/sessions`` (override with
``PI_CODING_AGENT_SESSION_DIR`` or ``PI_CODING_AGENT_DIR``). Each line is a
session entry. Conversational turns are ``type:"message"`` with an
``message.role`` of ``user`` or ``assistant``. Compaction appends a
``type:"compaction"`` entry and leaves the raw messages in the file, so this
adapter skips compaction entries instead of indexing the summary a second time.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.schema import AssistantRecord, Block, Record, ToolUseRef, UserRecord
from lib.sources.base import SOURCES, SessionFile, SessionSource


def _sessions_root() -> Path:
    explicit = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if explicit:
        return Path(explicit).expanduser()
    agent = os.environ.get("PI_CODING_AGENT_DIR")
    if agent:
        return Path(agent).expanduser() / "sessions"
    return Path.home() / ".pi" / "agent" / "sessions"


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_from_content(content: Any) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str) and block.strip():
            parts.append(block.strip())
        elif isinstance(block, dict) and block.get("type") in ("text", "output_text"):
            piece = str(block.get("text") or "").strip()
            if piece:
                parts.append(piece)
    joined = "\n".join(parts).strip()
    return joined or None


def _tool_blocks(content: Any) -> list[Block]:
    if not isinstance(content, list):
        return []
    out: list[Block] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind not in ("toolCall", "tool_use", "functionCall"):
            continue
        name = block.get("name") or block.get("toolName") or ""
        args = block.get("arguments") or block.get("input") or {}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append(
            Block(
                type="tool_use",
                tool_use=ToolUseRef(
                    id=str(block.get("id") or ""),
                    name=str(name),
                    input=args,
                ),
                raw=block,
            )
        )
    return out


def _translate(obj: dict[str, Any], session: SessionFile, byte_offset: int) -> Record | None:
    kind = obj.get("type")
    if kind != "message":
        # Compaction, model changes, and our own post-compact custom message
        # are control-flow. Raw user/assistant lines stay in the file.
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in ("user", "assistant"):
        return None
    content = message.get("content")
    text = _text_from_content(content)
    entry_id = obj.get("id")
    uuid = entry_id if isinstance(entry_id, str) else None
    parent = obj.get("parentId")
    base: dict[str, Any] = dict(
        type=role,
        uuid=uuid,
        parent_uuid=parent if isinstance(parent, str) else None,
        session_id=session.session_id,
        ts=_parse_ts(obj.get("timestamp")),
        cwd=session.cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw=obj,
        byte_offset=byte_offset,
    )
    if role == "assistant":
        blocks: list[Block] = []
        if text:
            blocks.append(Block(type="text", text=text))
        blocks.extend(_tool_blocks(content))
        return AssistantRecord(
            **base,
            model=message.get("model") if isinstance(message.get("model"), str) else None,
            content=blocks,
            usage=None,
            stop_reason=None,
            message_id=uuid,
            request_id=None,
        )
    return UserRecord(
        **base,
        content_kind="string" if text else "empty",
        text=text,
        tool_results=[],
        tool_use_result_payload=None,
        is_compact_summary=False,
        is_meta=False,
    )


class PiSource(SessionSource):
    """Append-only Pi session JSONL."""

    name = "pi"

    def __init__(self, sessions_root: Path | None = None) -> None:
        self.sessions_root = Path(sessions_root) if sessions_root is not None else _sessions_root()

    def is_available(self) -> bool:
        root = self.sessions_root
        if not root.is_dir():
            return False
        try:
            return any(root.rglob("*.jsonl"))
        except OSError:
            return False

    def discover_sessions(self) -> Iterator[SessionFile]:
        root = self.sessions_root
        if not root.is_dir():
            return
        try:
            paths = sorted(p for p in root.rglob("*.jsonl") if p.is_file())
        except OSError:
            return
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            yield SessionFile(
                source=self.name,
                path=path,
                cwd=None,
                session_id=path.stem,
                started_at=None,
                last_modified=stat.st_mtime,
                extra={},
            )

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        path = Path(session.path)
        try:
            handle = path.open("rb")
        except OSError:
            return
        with handle:
            if start_offset:
                handle.seek(start_offset)
            while True:
                line = handle.readline()
                if not line:
                    break
                next_offset = handle.tell()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                rec = _translate(obj, session, next_offset - len(line))
                if rec is not None:
                    yield next_offset, rec


SOURCES.append(PiSource)

__all__ = ["PiSource"]
