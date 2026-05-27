"""Continue (continue.dev) adapter.

Continue is a VS Code / JetBrains AI assistant that stores chat sessions
as JSON files in a global directory. Both editor integrations share the
same on-disk layout, so one adapter covers both.

Storage layout
==============

Root: ``$CONTINUE_GLOBAL_DIR/sessions`` if the env var is set, otherwise
``~/.continue/sessions``.

* ``sessions.json`` — an index array of ``BaseSessionMetadata`` objects.
  Used for cheap title / mtime lookups during discovery.
* ``<sessionId>.json`` — one per session, conforming to the ``Session``
  TS interface in continue's ``core/index.d.ts:279-298``::

      interface Session {
        sessionId: string;
        title: string;
        workspaceDirectory: string;
        history: ChatHistoryItem[];
        mode?: MessageModes;
        chatModelTitle?: string | null;
        usage?: SessionUsage;
      }
      interface ChatHistoryItem {
        message: ChatMessage;             // role + content (Anthropic-style)
        contextItems: ContextItemWithId[];
        editorState?: any;
        modifiers?: InputModifiers;
        promptLogs?: PromptLog[];
        toolCallStates?: ToolCallState[];
        isGatheringContext?: boolean;
        reasoning?: Reasoning;
        appliedRules?: RuleMetadata[];
        conversationSummary?: string;
      }

Translation
===========

Each ``ChatHistoryItem`` becomes one canonical :class:`lib.schema.Record`:

* ``message.role == "user"`` → :class:`UserRecord`. Tool results (whether
  carried as Anthropic-style ``tool_result`` blocks inside the content
  array or as plain text) are normalized to ``content_kind="tool_result"``
  when discernible, else ``"string"`` / ``"text_array"``.
* ``message.role == "assistant"`` → :class:`AssistantRecord`. Tool calls
  come from one of two places: ``toolCallStates[]`` (Continue's normalized
  store) or unwrapped from ``message.content`` if the assistant message
  carries Anthropic-style ``tool_use`` blocks. ``chatModelTitle`` is used
  as the model when present.
* ``message.role == "system"`` → :class:`SystemRecord` with
  ``subtype="continue_system"``.
* Anything else falls through to base :class:`Record`.

``Session.usage`` (when present) is attached to the *first* assistant
record's ``usage`` field so token accounting works the same way Claude
Code does it.

The byte offset returned by :meth:`iter_records` is the *index into the
history array* (not a real byte offset) — Continue rewrites the whole
file on every turn, so byte-offset checkpointing is meaningless. Indexes
are still monotonic per session and let the ingest loop resume.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator, Optional

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    SystemRecord,
    ToolResult,
    ToolUseRef,
    UserRecord,
    _classify_user_content,
    _extract_tool_results,
    _extract_user_text,
    _parse_ts,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_sessions_dir() -> Path:
    """Return ``$CONTINUE_GLOBAL_DIR/sessions`` or ``~/.continue/sessions``."""

    env = os.environ.get("CONTINUE_GLOBAL_DIR")
    if env:
        return Path(env).expanduser() / "sessions"
    return Path.home() / ".continue" / "sessions"


def _safe_load_json(path: Path) -> Optional[Any]:
    """Return parsed JSON or ``None`` on any read / parse error."""

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _content_to_text(content: Any) -> Optional[str]:
    """Flatten Continue's polymorphic ``message.content`` to a string.

    Continue messages may carry ``content`` as a plain string, an
    Anthropic-style list of ``{type, text}`` blocks, or a list that mixes
    ``tool_use`` / ``tool_result`` entries. This is a string-only view —
    callers should consult the raw content for structured blocks.
    """

    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        return _extract_user_text(content)
    return None


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _build_base_kwargs(
    raw: dict[str, Any],
    *,
    session_id: str,
    cwd: Optional[str],
    index: int,
) -> dict[str, Any]:
    """Populate the cross-cutting Record fields from a ``ChatHistoryItem``."""

    msg = raw.get("message") or {}
    prompt_logs = raw.get("promptLogs") or []
    ts = None
    # ``promptLogs[0].timestamp`` is the closest thing to a per-turn timestamp.
    if isinstance(prompt_logs, list) and prompt_logs:
        first = prompt_logs[0]
        if isinstance(first, dict):
            ts = _parse_ts(first.get("timestamp"))

    return dict(
        type=msg.get("role", "?") if isinstance(msg, dict) else "?",
        uuid=msg.get("id") if isinstance(msg, dict) else None,
        parent_uuid=None,  # Continue history is implicitly ordered by index.
        session_id=session_id,
        ts=ts,
        cwd=cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw=raw,
        byte_offset=index,
    )


def _unwrap_assistant_blocks(content: Any) -> list[Block]:
    """Convert Anthropic-style content to :class:`Block` instances."""

    out: list[Block] = []
    if isinstance(content, str):
        if content:
            out.append(Block(type="text", text=content, raw={"text": content}))
        return out
    if not isinstance(content, list):
        return out
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            out.append(Block(type=bt, text=b.get("text", ""), raw=b))
        elif bt == "thinking":
            out.append(
                Block(
                    type=bt,
                    thinking=b.get("thinking", ""),
                    thinking_signature=b.get("signature"),
                    raw=b,
                )
            )
        elif bt == "tool_use":
            out.append(
                Block(
                    type=bt,
                    tool_use=ToolUseRef(
                        id=b.get("id", "") or "",
                        name=b.get("name", "") or "",
                        input=b.get("input", {}) or {},
                    ),
                    raw=b,
                )
            )
        else:
            out.append(Block(type=bt or "?", raw=b))
    return out


def _blocks_from_tool_call_states(states: Any) -> list[Block]:
    """Convert Continue's ``toolCallStates[]`` into ``tool_use`` blocks.

    Each state has shape ``{toolCallId, toolCall: {function: {name, arguments}}}``
    (loose — Continue rewrites this between versions). We try to be liberal
    in what we accept.
    """

    out: list[Block] = []
    if not isinstance(states, list):
        return out
    for st in states:
        if not isinstance(st, dict):
            continue
        call_id = st.get("toolCallId") or ""
        tc = st.get("toolCall") or {}
        if not isinstance(tc, dict):
            tc = {}
        fn = tc.get("function") or {}
        name = ""
        args: Any = {}
        if isinstance(fn, dict):
            name = fn.get("name", "") or ""
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except ValueError:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
        out.append(
            Block(
                type="tool_use",
                tool_use=ToolUseRef(id=call_id, name=name, input=args or {}),
                raw=st,
            )
        )
    return out


def _project_item_to_record(
    item: dict[str, Any],
    *,
    session_id: str,
    cwd: Optional[str],
    index: int,
    chat_model_title: Optional[str],
    usage_for_first_assistant: Optional[dict[str, Any]],
) -> Record:
    """Translate one ``ChatHistoryItem`` into a canonical Record."""

    msg = item.get("message") or {}
    role = msg.get("role") if isinstance(msg, dict) else None
    base = _build_base_kwargs(item, session_id=session_id, cwd=cwd, index=index)
    content = msg.get("content") if isinstance(msg, dict) else None

    if role == "assistant":
        blocks = _unwrap_assistant_blocks(content)
        # Prefer toolCallStates when the content didn't already carry them.
        if not any(b.type == "tool_use" for b in blocks):
            blocks.extend(_blocks_from_tool_call_states(item.get("toolCallStates")))
        return AssistantRecord(
            **base,
            model=chat_model_title,
            content=blocks,
            usage=usage_for_first_assistant,
            stop_reason=None,
            message_id=msg.get("id") if isinstance(msg, dict) else None,
            request_id=None,
        )

    if role == "user":
        kind = _classify_user_content(content)
        text = _content_to_text(content)
        results = _extract_tool_results(content) if kind == "tool_result" else []
        # Continue sometimes carries tool *results* as plain text inside the
        # user message; we can't recover the tool_use_id from that. Leave
        # them as ``content_kind="string"`` in that case.
        return UserRecord(
            **base,
            content_kind=kind,
            text=text,
            tool_results=results,
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=False,
        )

    if role == "system":
        payload = {"message": msg, "contextItems": item.get("contextItems")}
        return SystemRecord(**base, subtype="continue_system", payload=payload)

    # Unknown role — return a bare Record so the pipeline still sees it.
    return Record(**base)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ContinueSource(SessionSource):
    """Adapter for Continue (continue.dev) chat sessions.

    Construct with no arguments in production. Tests may pass
    ``sessions_dir`` directly to point at a synthetic tree. The
    ``$CONTINUE_GLOBAL_DIR`` environment variable, when set, overrides
    both — matching Continue's own resolution order.
    """

    name = "continue"

    def __init__(self, sessions_dir: Optional[Path] = None) -> None:
        env = os.environ.get("CONTINUE_GLOBAL_DIR")
        if env:
            self.sessions_dir = Path(env).expanduser() / "sessions"
        elif sessions_dir is not None:
            self.sessions_dir = sessions_dir
        else:
            self.sessions_dir = Path.home() / ".continue" / "sessions"

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """``True`` if the sessions dir contains an index or any session JSON.

        Cheap: only checks directory existence + a single ``glob`` short-
        circuit. No JSON parsing.
        """

        if not self.sessions_dir.is_dir():
            return False
        if (self.sessions_dir / "sessions.json").is_file():
            return True
        return any(self.sessions_dir.glob("*.json"))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per Continue session JSON.

        We read ``sessions.json`` for cheap title / workspace metadata.
        Sessions present on disk but missing from the index are still
        surfaced (some Continue versions write the per-session file before
        updating the index). Order is by session id, sorted.
        """

        if not self.sessions_dir.is_dir():
            return

        index_path = self.sessions_dir / "sessions.json"
        index_by_id: dict[str, dict[str, Any]] = {}
        idx = _safe_load_json(index_path)
        if isinstance(idx, list):
            for entry in idx:
                if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
                    index_by_id[entry["sessionId"]] = entry

        # Collect session files. We deliberately skip ``sessions.json``.
        session_paths: dict[str, Path] = {}
        for p in sorted(self.sessions_dir.glob("*.json")):
            if p.name == "sessions.json":
                continue
            if not p.is_file():
                continue
            session_paths[p.stem] = p

        # Also include index-only entries (file may have been pruned).
        for sid in index_by_id:
            if sid not in session_paths:
                candidate = self.sessions_dir / f"{sid}.json"
                if candidate.is_file():
                    session_paths[sid] = candidate

        for sid in sorted(session_paths):
            path = session_paths[sid]
            try:
                stat = path.stat()
            except OSError:
                continue
            meta = index_by_id.get(sid, {})
            workspace = meta.get("workspaceDirectory") if isinstance(meta, dict) else None
            title = meta.get("title") if isinstance(meta, dict) else None
            date_created = meta.get("dateCreated") if isinstance(meta, dict) else None
            started_at: Optional[float] = None
            if isinstance(date_created, (int, float)):
                # Continue records ms epoch.
                started_at = float(date_created) / 1000.0
            yield SessionFile(
                source=self.name,
                path=path,
                cwd=workspace if isinstance(workspace, str) else None,
                session_id=sid,
                started_at=started_at,
                last_modified=stat.st_mtime,
                extra={"title": title} if title else {},
            )

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Yield ``(next_index, Record)`` for every history item.

        ``start_offset`` is treated as the index of the *next* history item
        to emit (Continue rewrites the whole file each turn, so byte
        offsets are meaningless). Passing the value returned by the last
        yield resumes from the following item.
        """

        body = _safe_load_json(session.path)
        if not isinstance(body, dict):
            return

        history = body.get("history")
        if not isinstance(history, list):
            return

        chat_model_title = body.get("chatModelTitle")
        if not isinstance(chat_model_title, str):
            chat_model_title = None

        usage = body.get("usage")
        usage_dict = usage if isinstance(usage, dict) else None

        # Workspace from the file body wins over discovery-time hint.
        cwd = body.get("workspaceDirectory")
        if not isinstance(cwd, str):
            cwd = session.cwd

        session_id = body.get("sessionId")
        if not isinstance(session_id, str):
            session_id = session.session_id

        first_assistant_seen = False
        start = max(0, int(start_offset))
        for i in range(start, len(history)):
            item = history[i]
            if not isinstance(item, dict):
                continue
            attach_usage: Optional[dict[str, Any]] = None
            role = (item.get("message") or {}).get("role") if isinstance(
                item.get("message"), dict
            ) else None
            if role == "assistant" and not first_assistant_seen and usage_dict is not None:
                attach_usage = usage_dict
                first_assistant_seen = True
            rec = _project_item_to_record(
                item,
                session_id=session_id,
                cwd=cwd,
                index=i,
                chat_model_title=chat_model_title,
                usage_for_first_assistant=attach_usage,
            )
            yield (i + 1, rec)


# Register at import time.
SOURCES.append(ContinueSource)


__all__ = ["ContinueSource"]
