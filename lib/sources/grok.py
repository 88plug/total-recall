"""Grok CLI session-source adapter — per-session JSONL chat history.

Grok CLI (xAI's ``grok`` coding agent, a Cursor-derived "Composer" client)
stores its state under ``~/.grok`` (overridable via the ``GROK_HOME`` env
var). Sessions are keyed by working directory and session id::

    ~/.grok/sessions/<url-encoded-cwd>/<session-uuid>/chat_history.jsonl

The directory immediately under ``sessions/`` is the **cwd**, percent-encoded
with :func:`urllib.parse.quote` using ``safe=''`` — so every ``/`` becomes
``%2F`` (e.g. ``/home/operator/my-project`` →
``%2Fhome%2Foperator%2Fmy-project``). :func:`urllib.parse.unquote`
recovers the original path exactly (round-trips spaces, ``+``, and non-ASCII).
Each child directory is a session UUID (ULID-shaped UUIDv7, e.g.
``019ef1be-99af-7df2-b8bf-45a9a80ddebc``) holding the dense transcript plus
sidecar metadata: ``summary.json``, ``events.jsonl`` (telemetry, not
ingested), ``updates.jsonl``, ``rewind_points.jsonl``, ``signals.json``,
``prompt_context.json``, ``system_prompt.txt``, and ``subagents/<id>/meta.json``.

Sub-agent runs are recorded as their own session UUID dirs nested under
``<parent>/subagents/<child-id>/`` carrying only a ``meta.json``; the
sub-agent's actual transcript appears as a normal top-level session dir under
the same cwd, cross-referenced by id — so a recursive ``chat_history.jsonl``
walk picks each transcript up exactly once.

What ``chat_history.jsonl`` carries (the dense signal — both halves of the
conversation, not just user prompts):

* ``system``     — the system prompt (one per session). ``content`` is a str.
* ``user``       — a user / tool-feedback turn. ``content`` is a list of
                   ``{"type":"text","text":...}`` blocks (occasionally a bare
                   string). Carries ``synthetic_reason`` when Grok injected it.
* ``reasoning``  — the model's private reasoning. ``summary`` is a list of
                   ``{"type":"summary_text","text":...}`` blocks; the verbatim
                   chain lives in opaque ``encrypted_content`` plus a
                   ``status`` field.
* ``assistant``  — a model turn. ``content`` is **always a string** (the
                   visible answer); tool calls live in a *sibling*
                   ``tool_calls`` array of ``{"id","name","arguments"}`` where
                   ``arguments`` is a JSON-encoded **string** needing a second
                   ``json.loads`` (same quirk as Codex FunctionCall). Plus
                   ``model_id`` and ``model_fingerprint``.
* ``tool_result``— its own **top-level** record keyed back to the call via
                   ``tool_call_id`` (not nested in a user message as in the
                   Anthropic wire format). ``content`` is a string.

**No per-message timestamps, uuids, or parent links.** ``chat_history.jsonl``
records are bare — no ``ts``, ``uuid``, ``parentUuid``, ``cwd``, or version per
line. ``Record.ts`` is therefore always ``None`` (we never fabricate a
per-turn timestamp from "now"). Session-level context is recovered from
sidecars: ``cwd`` from ``summary.json``'s ``info.cwd`` (url-decoded dir name as
fallback), the session ``started_at`` from ``summary.json``'s ``created_at``,
and the active model from ``current_model_id``.

``prompt_history.jsonl`` is a **per-workspace** sibling of the session dirs
(not a session itself). Its schema: ``timestamp`` (RFC-3339 UTC, nanosecond
precision), ``session_id`` (owning UUID), ``prompt`` (raw text), ``is_bash``
(bool; ``true`` for a ``!``-prefixed shell escape). It duplicates the user
half of the transcript with wall-clock timestamps; not currently ingested.

Read-only: the adapter never writes into ``~/.grok``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    SystemRecord,
    ToolResult,
    ToolUseRef,
    UserRecord,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource

CHAT_HISTORY = "chat_history.jsonl"
SUMMARY = "summary.json"
PROMPT_HISTORY = "prompt_history.jsonl"


def derive_cwd_from_dir(dir_name: str) -> str:
    """Decode a percent-encoded ``sessions/`` child dir back to its cwd.

    Inverse of :func:`derive_dir_from_cwd`. ``%2Fhome%2Foperator`` →
    ``/home/operator``.
    """

    return unquote(dir_name)


def derive_dir_from_cwd(cwd: str) -> str:
    """Encode a cwd into the ``sessions/`` child dir name Grok uses.

    Every byte that is not URL-unreserved is percent-encoded (``safe=''``),
    so path separators become ``%2F``.
    """

    return quote(cwd, safe="")


def _default_sessions_root() -> Path:
    """``$GROK_HOME/sessions`` when ``GROK_HOME`` is set, else ``~/.grok/sessions``."""

    grok_home = os.environ.get("GROK_HOME")
    base = Path(grok_home) if grok_home else Path.home() / ".grok"
    return base / "sessions"


def _parse_ts(raw: Any) -> datetime | None:
    """Best-effort RFC-3339 → tz-aware :class:`datetime`.

    Grok writes nanosecond precision and a ``Z`` suffix
    (``2026-06-15T12:16:24.884900078Z``); Python only takes microseconds, so
    we truncate the fractional part to 6 digits before parsing.
    """

    if not isinstance(raw, str) or not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{frac[:6]}{rest}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class GrokSource(SessionSource):
    """Adapter for ``~/.grok/sessions/<enc-cwd>/<uuid>/chat_history.jsonl``.

    The sessions-root is overridable for tests via the constructor; otherwise
    it follows ``$GROK_HOME/sessions`` (or ``~/.grok/sessions``). An explicit
    constructor argument takes precedence over the env var.
    """

    name = "grok"

    def __init__(self, sessions_root: Path | None = None) -> None:
        self.sessions_root = (
            sessions_root if sessions_root is not None else _default_sessions_root()
        )

    # ---- availability ---------------------------------------------------

    def is_available(self) -> bool:
        """``True`` iff at least one ``chat_history.jsonl`` exists on disk.

        Stays cheap by short-circuiting on the first match. An empty
        ``sessions/`` dir (no sessions yet) reports ``False`` so the CLI
        skips a Grok install that has never recorded a conversation.
        """

        if not self.sessions_root.is_dir():
            return False
        try:
            for enc_dir in self.sessions_root.iterdir():
                if not enc_dir.is_dir():
                    continue
                for _ in enc_dir.rglob(CHAT_HISTORY):
                    return True
        except OSError:
            return False
        return False

    # ---- discovery ------------------------------------------------------

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per ``chat_history.jsonl``.

        Walks ``<enc-cwd>/<uuid>/`` session dirs **and** their nested
        ``subagents/<child-uuid>/`` dirs (a sub-agent that produced a
        transcript). Order is cwd-dir then session-uuid, both sorted, so
        callers can checkpoint. ``cwd`` comes from ``summary.json``'s
        ``info.cwd`` when present, falling back to url-decoding the workspace
        dir name; sidecar-read failures degrade to the fallback rather than
        dropping the session.
        """

        if not self.sessions_root.is_dir():
            return
        for enc_dir in sorted(self.sessions_root.iterdir()):
            if not enc_dir.is_dir():
                continue
            fallback_cwd = derive_cwd_from_dir(enc_dir.name)
            for chat in sorted(enc_dir.rglob(CHAT_HISTORY)):
                if not chat.is_file():
                    continue
                try:
                    stat = chat.stat()
                except OSError:
                    continue
                session_dir = chat.parent
                session_id = session_dir.name
                is_sidechain = session_dir.parent.name == "subagents"
                parent_session = session_dir.parent.parent.name if is_sidechain else None

                summary = _read_summary(session_dir / SUMMARY)
                cwd = summary.get("cwd") or fallback_cwd
                started_at = summary.get("started_at")
                model = summary.get("model")
                title = summary.get("title")

                extra: dict[str, Any] = {
                    "enc_dir": enc_dir.name,
                    "is_sidechain": is_sidechain,
                    "parent_session_id": parent_session,
                }
                if model is not None:
                    extra["model"] = model
                if title is not None:
                    extra["title"] = title

                yield SessionFile(
                    source=self.name,
                    path=chat,
                    cwd=cwd,
                    session_id=session_id,
                    started_at=started_at,
                    last_modified=stat.st_mtime,
                    extra=extra,
                )

    # ---- record streaming ----------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Record]]:
        """Stream ``(next_byte_offset, Record)`` pairs from ``chat_history``.

        Standard JSONL byte-offset streaming so the offset is a real resume
        cursor (consistent with the other JSONL sources). Each line is
        translated into the canonical :class:`lib.schema.Record` subclass:

        * ``system``     → :class:`SystemRecord` (subtype ``"system_prompt"``)
        * ``user``       → :class:`UserRecord` (``content_kind="string"``)
        * ``assistant``  → :class:`AssistantRecord` (visible text + tool_use
                            blocks synthesised from ``tool_calls``)
        * ``reasoning``  → :class:`AssistantRecord` (a single ``thinking``
                            block built from the ``summary`` text)
        * ``tool_result``→ :class:`UserRecord` (``content_kind="tool_result"``)

        Blank lines and malformed JSON are skipped, not fatal. Unknown
        ``type`` values fall through to the base :class:`Record` so nothing
        is silently lost and the adapter is forward-compatible with additive
        Grok changes. ``Record.ts`` is always ``None`` — chat lines carry no
        per-turn timestamp and we never fabricate one.
        """

        extra = session.extra or {}
        model = extra.get("model")
        is_sidechain = bool(extra.get("is_sidechain"))

        try:
            f = session.path.open("rb")
        except OSError:
            return
        with f:
            f.seek(start_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                next_offset = f.tell()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                rec = _translate(
                    obj,
                    session=session,
                    model=model,
                    is_sidechain=is_sidechain,
                    byte_offset=next_offset - len(line),
                )
                if rec is not None:
                    yield next_offset, rec


# ---------------------------------------------------------------------------
# Sidecar reading
# ---------------------------------------------------------------------------


def _read_summary(summary_file: Path) -> dict[str, Any]:
    """Pull ``cwd`` / ``started_at`` / ``model`` / ``title`` from ``summary.json``.

    Returns a dict whose keys are only present when the corresponding field
    parsed cleanly, so callers can ``.get()`` and fall back. ``cwd`` comes
    from ``info.cwd``, ``started_at`` from ``created_at`` (unix seconds),
    ``model`` from ``current_model_id``, ``title`` from ``session_summary``
    (or ``generated_title``).
    """

    if not summary_file.is_file():
        return {}
    try:
        obj = json.loads(summary_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(obj, dict):
        return {}

    out: dict[str, Any] = {}
    info = obj.get("info")
    if isinstance(info, dict) and isinstance(info.get("cwd"), str):
        out["cwd"] = info["cwd"]
    dt = _parse_ts(obj.get("created_at"))
    if dt is not None:
        out["started_at"] = dt.timestamp()
    model = obj.get("current_model_id")
    if isinstance(model, str):
        out["model"] = model
    title = obj.get("session_summary") or obj.get("generated_title")
    if isinstance(title, str) and title:
        out["title"] = title
    return out


# ---------------------------------------------------------------------------
# Record translation
# ---------------------------------------------------------------------------


def _base_kwargs(
    obj: dict[str, Any],
    session: SessionFile,
    is_sidechain: bool,
    byte_offset: int,
) -> dict[str, Any]:
    """Shared :class:`Record` base fields for one Grok chat record.

    Grok's chat lines carry no uuid / parent / git / version / per-line
    timestamp, so those stay ``None``. ``cwd`` is the resolved session cwd;
    ``raw`` keeps the full line for downstream miners.
    """

    return dict(
        type=str(obj.get("type") or "?"),
        uuid=None,
        parent_uuid=None,
        session_id=session.session_id,
        ts=None,
        cwd=session.cwd,
        git_branch=None,
        version=None,
        is_sidechain=is_sidechain,
        raw=obj,
        byte_offset=byte_offset,
    )


def _summary_text(summary: Any) -> str:
    """Flatten a ``reasoning.summary`` list into one string."""

    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if isinstance(item, dict):
            t = item.get("text")
            if isinstance(t, str):
                parts.append(t)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _user_text(content: Any) -> str | None:
    """Extract plain text from a Grok ``user`` content payload.

    ``content`` is usually a list of ``{"type":"text","text":...}`` blocks,
    occasionally a bare string.
    """

    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else None
    return None


def _tool_use_blocks(tool_calls: Any) -> list[Block]:
    """Convert Grok ``assistant.tool_calls`` into ``tool_use`` blocks.

    Each call is ``{"id","name","arguments"}`` where ``arguments`` is a JSON
    string; we double-decode it into a dict (falling back to ``{"raw": <str>}``
    when it isn't valid JSON) to match the :class:`ToolUseRef` shape. The
    input is always a dict, never a crash.
    """

    blocks: list[Block] = []
    if not isinstance(tool_calls, list):
        return blocks
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        args = call.get("arguments")
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except json.JSONDecodeError:
                parsed = {"raw": args}
        elif isinstance(args, dict):
            parsed = args
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        blocks.append(
            Block(
                type="tool_use",
                tool_use=ToolUseRef(
                    id=str(call.get("id") or ""),
                    name=str(call.get("name") or ""),
                    input=parsed,
                ),
                raw=call,
            )
        )
    return blocks


def _translate(
    obj: dict[str, Any],
    *,
    session: SessionFile,
    model: str | None,
    is_sidechain: bool,
    byte_offset: int,
) -> Record | None:
    """Translate one parsed Grok chat record into a canonical Record."""

    rtype = obj.get("type")
    base = _base_kwargs(obj, session, is_sidechain, byte_offset)

    if rtype == "system":
        base["type"] = "system"
        return SystemRecord(
            **base,
            subtype="system_prompt",
            payload={"content": obj.get("content")},
        )

    if rtype == "user":
        return UserRecord(
            **base,
            content_kind="string",
            text=_user_text(obj.get("content")),
            tool_results=[],
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=obj.get("synthetic_reason") is not None,
        )

    if rtype == "tool_result":
        content = obj.get("content")
        content_str = content if isinstance(content, str) else json.dumps(content)
        return UserRecord(
            **base,
            content_kind="tool_result",
            text=None,
            tool_results=[
                ToolResult(
                    tool_use_id=str(obj.get("tool_call_id") or ""),
                    is_error=False,
                    content=content_str,
                    raw_content=content,
                )
            ],
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=False,
        )

    if rtype == "assistant":
        blocks: list[Block] = []
        text = obj.get("content")
        if isinstance(text, str) and text:
            blocks.append(Block(type="text", text=text))
        blocks.extend(_tool_use_blocks(obj.get("tool_calls")))
        return AssistantRecord(
            **base,
            model=obj.get("model_id") or model,
            content=blocks,
            usage=None,
            stop_reason=None,
            message_id=None,
            request_id=None,
        )

    if rtype == "reasoning":
        thinking = _summary_text(obj.get("summary"))
        return AssistantRecord(
            **base,
            model=model,
            content=[Block(type="thinking", thinking=thinking)] if thinking else [],
            usage=None,
            stop_reason=None,
            message_id=str(obj.get("id")) if obj.get("id") else None,
            request_id=None,
        )

    # Unknown type — preserve as a base Record so nothing is silently lost.
    return Record(**base)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

SOURCES.append(GrokSource)

__all__ = [
    "GrokSource",
    "derive_cwd_from_dir",
    "derive_dir_from_cwd",
]
