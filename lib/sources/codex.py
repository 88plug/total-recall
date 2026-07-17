"""OpenAI Codex CLI session adapter.

Codex stores its rollouts on disk as JSONL files at::

    $CODEX_HOME/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDThh-mm-ss-<thread-uuid>.jsonl

``$CODEX_HOME`` defaults to ``~/.codex``. There is a sidecar SQLite at
``state_db.rs`` for thread *listing*, but the JSONL files are canonical for
record content — and the date-tree layout makes incremental tailing cheap
(``mtime`` sort within the most recent day's directory).

Wire format
-----------

Each line is a ``RolloutLine`` with three top-level keys::

    {"timestamp": "...", "type": "<variant>", "payload": {...}}

(Serde does ``#[serde(flatten)]`` on the payload, but the upstream Python
adapters in this project model it as nested. Either way the round-trip is
straightforward.)

``type`` is one of:

* ``session_meta``     — first record, sets ``cwd``, ``model_provider``,
                         ``originator``, ``cli_version``, ``source``,
                         session id (``id``).
* ``response_item``    — workhorse; payload is itself a tagged variant.
  Sub-variants observed in ``codex-rs/protocol/src/models.rs``:

      Message, FunctionCall, FunctionCallOutput, LocalShellCall,
      CustomToolCall, ToolSearchCall, Reasoning, EncryptedReasoning,
      WebSearchCall, ImageGenerationCall, FileSearchCall

* ``turn_context``     — ``{model, effort, personality, approval_policy,
                          sandbox_policy, cwd}`` per turn. The ``model``
                          field can change mid-session, so the adapter
                          threads the most-recent value forward into each
                          subsequent ``response_item``.
* ``compacted``        — history replacement; mid-session. Subsequent
                          replay is against ``replacement_history``;
                          records prior to a ``compacted`` line in the
                          same file should be marked superseded (we yield
                          a SystemRecord with ``subtype="compact_boundary"``
                          and emit a fresh stream from
                          ``replacement_history`` if present).
* ``event_msg``        — arbitrary system events; includes ``token_count``
                          events whose payload carries the rolled-up
                          token totals.

Translation to ``lib.schema.Record``
-----------------------------------

The downstream pipeline consumes the canonical ``Record`` family from
``lib.schema``. Codex maps cleanly onto those types:

* ``session_meta``       → ``SystemRecord(subtype="session_meta")``;
                           ``cwd`` and ``session_id`` populated.
* ``turn_context``       → consumed for internal state (current model,
                           current cwd); also yielded as
                           ``SystemRecord(subtype="turn_context")`` so
                           downstream can observe model switches.
* ``compacted``          → ``SystemRecord(subtype="compact_boundary")``;
                           the inner ``replacement_history`` (if any) is
                           replayed as fresh records in order.
* ``event_msg``          → ``SystemRecord(subtype="event_msg:" + inner)``
                           with token-count events normalised into
                           ``payload["usage"]`` using our cross-source
                           token field names (see ``_normalize_tokens``).
* ``response_item``      → see ``_translate_response_item``. Message
                           variants become AssistantRecord / UserRecord,
                           tool calls become AssistantRecord blocks, tool
                           outputs become UserRecord(tool_result).

Token field mapping
-------------------

OpenAI's usage schema is::

    {input_tokens, cached_input_tokens, output_tokens,
     reasoning_output_tokens, total_tokens}

There is no ``cache_creation_tokens`` (OpenAI doesn't expose it). We map::

    input_tokens             → input_tokens
    cached_input_tokens      → cache_read_tokens
    output_tokens            → output_tokens
    reasoning_output_tokens  → cache_creation_tokens   (closest analog)
    total_tokens             → total_tokens

The original keys are preserved verbatim under ``usage["_codex_raw"]``.

FunctionCall arguments quirk
----------------------------

In Codex JSONL, ``response_item.payload.FunctionCall.arguments`` is itself
a JSON-encoded **string**, not a structured object — i.e. ``json.loads``
the line gets you the FunctionCall dict, but you must ``json.loads`` the
``arguments`` value a second time to get the actual call args. We do
that translation here so downstream sees a plain dict in
``Block.tool_use.input``.

FunctionCallOutput dual shape
-----------------------------

``response_item.payload.FunctionCallOutput.output`` is either a plain
string or a structured ``{content_items: [...]}`` object. Both shapes
are normalised here to the ``ToolResult.content`` (string) /
``ToolResult.raw_content`` (original) split.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

DEFAULT_CODEX_HOME = Path("~/.codex").expanduser()


# ---------------------------------------------------------------------------
# token field mapping
# ---------------------------------------------------------------------------


def _normalize_tokens(usage: dict[str, Any]) -> dict[str, Any]:
    """Translate OpenAI/Codex usage keys to the project's cross-source names.

    The original payload is preserved under ``_codex_raw`` so callers that
    care about the raw OpenAI fields can still get at them.
    """
    out: dict[str, Any] = {"_codex_raw": dict(usage)}
    if "input_tokens" in usage:
        out["input_tokens"] = usage["input_tokens"]
    if "cached_input_tokens" in usage:
        out["cache_read_tokens"] = usage["cached_input_tokens"]
    if "output_tokens" in usage:
        out["output_tokens"] = usage["output_tokens"]
    if "reasoning_output_tokens" in usage:
        # Closest analog — Codex does not surface cache_creation.
        out["cache_creation_tokens"] = usage["reasoning_output_tokens"]
    if "total_tokens" in usage:
        out["total_tokens"] = usage["total_tokens"]
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _safe_json_loads(s: Any) -> Any:
    """``json.loads`` that returns the input unchanged on failure or non-string.

    Used for the FunctionCall.arguments double-decode where the inner
    value is *usually* a JSON-encoded string but Codex's older variants
    occasionally already contain a dict (forward-compat safety).
    """
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def _normalize_tool_output(output: Any) -> tuple[str, Any]:
    """Flatten the dual-shape FunctionCallOutput.output to (text, raw).

    Accepts either a plain string, or ``{content_items: [...]}`` (Codex's
    structured shape — items are dicts with a ``text`` / ``content`` key).
    """
    if isinstance(output, str):
        return output, output
    if isinstance(output, dict):
        items = output.get("content_items")
        if isinstance(items, list):
            parts: list[str] = []
            for it in items:
                if isinstance(it, dict):
                    txt = it.get("text") or it.get("content") or ""
                    if isinstance(txt, str):
                        parts.append(txt)
            return "\n".join(p for p in parts if p), output
        # Fallback: take a ``text`` field if there is one.
        if isinstance(output.get("text"), str):
            return output["text"], output
        return json.dumps(output, sort_keys=True), output
    return "", output


# ---------------------------------------------------------------------------
# translation
# ---------------------------------------------------------------------------


class _ReplayState:
    """Mutable state threaded through one JSONL stream's translation."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cwd: str | None = None
        self.model: str | None = None
        self.model_provider: str | None = None
        self.cli_version: str | None = None

    def base(self, obj: dict[str, Any], byte_offset: int) -> dict[str, Any]:
        return dict(
            type=obj.get("type", "?"),
            uuid=None,  # Codex line records have no per-record uuid.
            parent_uuid=None,
            session_id=self.session_id,
            ts=_parse_ts(obj.get("timestamp")),
            cwd=self.cwd,
            git_branch=None,
            version=self.cli_version,
            is_sidechain=False,
            raw=obj,
            byte_offset=byte_offset,
        )


def _translate_session_meta(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    state.session_id = payload.get("id") or state.session_id
    state.cwd = payload.get("cwd") or state.cwd
    state.model_provider = payload.get("model_provider") or state.model_provider
    state.cli_version = payload.get("cli_version") or state.cli_version
    base = state.base(obj, byte_offset)
    return SystemRecord(**base, subtype="session_meta", payload=dict(payload))


def _translate_turn_context(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    # Model can change mid-session — that's the whole point of turn_context.
    if isinstance(payload.get("model"), str):
        state.model = payload["model"]
    if isinstance(payload.get("cwd"), str):
        state.cwd = payload["cwd"]
    base = state.base(obj, byte_offset)
    return SystemRecord(**base, subtype="turn_context", payload=dict(payload))


def _translate_event_msg(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    """``event_msg`` payloads are tagged variants — pull out the inner kind."""
    inner_kind = ""
    inner_body: dict[str, Any] = {}
    if isinstance(payload, dict):
        # Two observed shapes:
        #   {"type": "token_count", ...rest}
        #   {"token_count": {...}}   (older Serde external tagging)
        if isinstance(payload.get("type"), str):
            inner_kind = payload["type"]
            inner_body = {k: v for k, v in payload.items() if k != "type"}
        else:
            # External-tagged: single key.
            for k, v in payload.items():
                inner_kind = k
                inner_body = v if isinstance(v, dict) else {"value": v}
                break

    normalized = dict(inner_body)
    if inner_kind == "token_count":
        # Locate token totals — historically nested under "info" or top-level.
        usage = inner_body.get("info") if isinstance(inner_body.get("info"), dict) else inner_body
        normalized["usage"] = _normalize_tokens(usage)

    base = state.base(obj, byte_offset)
    return SystemRecord(
        **base,
        subtype=f"event_msg:{inner_kind}" if inner_kind else "event_msg",
        payload=normalized,
    )


def _translate_message(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    role = payload.get("role")
    content = payload.get("content")

    # Codex content can be either a string or a list of `{type, text}` parts.
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
                elif isinstance(c.get("content"), str):
                    parts.append(c["content"])
            elif isinstance(c, str):
                parts.append(c)
        text = "\n".join(p for p in parts if p)
    elif isinstance(content, str):
        text = content
    else:
        text = ""

    base = state.base(obj, byte_offset)
    if role == "assistant":
        return AssistantRecord(
            **base,
            model=state.model,
            content=[Block(type="text", text=text, raw={"text": text})] if text else [],
            usage=None,
            stop_reason=None,
            message_id=payload.get("id"),
            request_id=None,
        )
    # Default to user (covers "user" and "system" prompts alike — the latter
    # is rare in codex sessions).
    return UserRecord(
        **base,
        content_kind="string",
        text=text or None,
        tool_results=[],
        tool_use_result_payload=None,
        is_compact_summary=False,
        is_meta=False,
    )


def _translate_function_call(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    """``FunctionCall`` → AssistantRecord with a single ``tool_use`` block."""
    raw_args = payload.get("arguments")
    args = _safe_json_loads(raw_args)
    if not isinstance(args, dict):
        args = {"_raw": args}
    name = payload.get("name", "")
    ns = payload.get("namespace")
    full_name = f"{ns}.{name}" if isinstance(ns, str) and ns else name
    call_id = payload.get("call_id") or payload.get("id") or ""
    block = Block(
        type="tool_use",
        tool_use=ToolUseRef(id=call_id, name=full_name, input=args),
        raw=dict(payload),
    )
    base = state.base(obj, byte_offset)
    return AssistantRecord(
        **base,
        model=state.model,
        content=[block],
        usage=None,
        stop_reason="tool_use",
        message_id=None,
        request_id=None,
    )


def _translate_local_shell_call(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    """``LocalShellCall`` is a separate variant from FunctionCall even
    though the shell tool is also a function tool. We collapse to the same
    ``tool_use`` block shape, but tag the name as ``local_shell`` so
    downstream can tell them apart from generic FunctionCalls."""
    call_id = payload.get("call_id") or payload.get("id") or ""
    # Local shell args are typically already structured (command list, cwd).
    args_field = payload.get("action") or payload.get("arguments") or {}
    if isinstance(args_field, str):
        args_field = _safe_json_loads(args_field)
    if not isinstance(args_field, dict):
        args_field = {"_raw": args_field}
    block = Block(
        type="tool_use",
        tool_use=ToolUseRef(id=call_id, name="local_shell", input=args_field),
        raw=dict(payload),
    )
    base = state.base(obj, byte_offset)
    return AssistantRecord(
        **base,
        model=state.model,
        content=[block],
        usage=None,
        stop_reason="tool_use",
        message_id=None,
        request_id=None,
    )


def _translate_function_call_output(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    """``FunctionCallOutput`` → UserRecord with one ``tool_result`` block."""
    call_id = payload.get("call_id") or ""
    text, raw_c = _normalize_tool_output(payload.get("output"))
    is_error = False
    if isinstance(payload.get("output"), dict):
        is_error = bool(payload["output"].get("is_error", False))
    tr = ToolResult(tool_use_id=call_id, is_error=is_error, content=text, raw_content=raw_c)
    base = state.base(obj, byte_offset)
    return UserRecord(
        **base,
        content_kind="tool_result",
        text=None,
        tool_results=[tr],
        tool_use_result_payload=raw_c if isinstance(raw_c, dict) else None,
        is_compact_summary=False,
        is_meta=False,
    )


def _translate_reasoning(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Record:
    """``Reasoning`` / ``EncryptedReasoning`` → AssistantRecord with a
    ``thinking`` block. EncryptedReasoning carries opaque bytes — we
    store them as the block ``thinking`` body, which downstream redacts."""
    is_encrypted = obj.get("payload", {}) is payload and obj.get("type") not in (
        "response_item",
    )  # defensive — caller already routed by sub-variant.

    text = ""
    if isinstance(payload, dict):
        text = (
            payload.get("text") or payload.get("content") or payload.get("encrypted_content") or ""
        )
        if isinstance(text, list):
            # Reasoning sometimes ships as [{summary: "..."}] segments.
            chunks: list[str] = []
            for seg in text:
                if isinstance(seg, dict):
                    s = seg.get("summary") or seg.get("text") or ""
                    if isinstance(s, str):
                        chunks.append(s)
            text = "\n".join(c for c in chunks if c)
    if not isinstance(text, str):
        text = str(text)

    block = Block(
        type="thinking",
        thinking=text,
        thinking_signature="encrypted" if is_encrypted else None,
        raw=dict(payload) if isinstance(payload, dict) else {"value": payload},
    )
    base = state.base(obj, byte_offset)
    return AssistantRecord(
        **base,
        model=state.model,
        content=[block],
        usage=None,
        stop_reason=None,
        message_id=None,
        request_id=None,
    )


def _translate_response_item(
    obj: dict[str, Any], payload: Any, state: _ReplayState, byte_offset: int
) -> Record | None:
    """Route on the inner tagged variant inside ``response_item.payload``."""
    if not isinstance(payload, dict):
        return None

    # Two observed wire shapes:
    #   {"type": "Message", "role": "...", ...}
    #   {"Message": {"role": "...", ...}}
    inner_type: str = ""
    inner_body: dict[str, Any] = {}
    if isinstance(payload.get("type"), str):
        inner_type = payload["type"]
        inner_body = {k: v for k, v in payload.items() if k != "type"}
    else:
        for k, v in payload.items():
            inner_type = k
            inner_body = v if isinstance(v, dict) else {"value": v}
            break

    routes = {
        "Message": _translate_message,
        "FunctionCall": _translate_function_call,
        "FunctionCallOutput": _translate_function_call_output,
        "LocalShellCall": _translate_local_shell_call,
        "Reasoning": _translate_reasoning,
        "EncryptedReasoning": _translate_reasoning,
    }
    handler = routes.get(inner_type)
    if handler is not None:
        return handler(obj, inner_body, state, byte_offset)

    # Unknown sub-variant (CustomToolCall, ToolSearchCall, future kinds) —
    # fall through to a generic SystemRecord so the byte_offset / cwd /
    # model context isn't lost. The full payload is in ``raw``.
    base = state.base(obj, byte_offset)
    return SystemRecord(**base, subtype=f"response_item:{inner_type}", payload=inner_body)


def _translate_compacted(
    obj: dict[str, Any], payload: dict[str, Any], state: _ReplayState, byte_offset: int
) -> Iterator[Record]:
    """Yield a compact_boundary marker, then replay any replacement_history.

    Codex's compaction replaces the in-memory history; the post-compaction
    transcript is what subsequent ``response_item`` lines build on top of.
    Records *before* this line are now superseded — we don't retroactively
    delete them, but downstream extractors can use the ``compact_boundary``
    marker as a session boundary and prefer the replacement segment.
    """
    base = state.base(obj, byte_offset)
    yield SystemRecord(**base, subtype="compact_boundary", payload=dict(payload))

    replacement = payload.get("replacement_history") if isinstance(payload, dict) else None
    if isinstance(replacement, list):
        for entry in replacement:
            if not isinstance(entry, dict):
                continue
            # Each entry is itself a response_item-shaped dict.
            rec = _translate_response_item(obj, entry, state, byte_offset)
            if rec is not None:
                yield rec


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------


class CodexSource(SessionSource):
    """Adapter for OpenAI Codex CLI sessions on disk.

    Default storage root is ``~/.codex/sessions``, overridable per-instance
    (tests) or via the ``$CODEX_HOME`` environment variable (production).
    """

    name = "codex"

    def __init__(self, codex_home: Path | None = None) -> None:
        if codex_home is not None:
            self.codex_home = Path(codex_home)
        else:
            env = os.environ.get("CODEX_HOME")
            self.codex_home = Path(env).expanduser() if env else DEFAULT_CODEX_HOME
        self.sessions_root = self.codex_home / "sessions"

    # -- discovery --------------------------------------------------------

    def is_available(self) -> bool:
        """Cheap on-disk check: ``sessions/`` exists and has at least one entry.

        We deliberately do not recurse — ``any(iterdir())`` is a single
        ``readdir`` syscall.
        """
        if not self.sessions_root.is_dir():
            return False
        try:
            return any(self.sessions_root.iterdir())
        except OSError:
            return False

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Walk ``sessions/YYYY/MM/DD/`` for ``rollout-*.jsonl`` files.

        Ordering is lexicographic on the full path, which — because the
        directory tree is date-formatted and the filenames embed a
        timestamp — is also chronological. Files whose ``stat()`` fails
        (broken symlink, race with cleanup) are silently skipped.
        """
        if not self.is_available():
            return
        for jsonl in sorted(self.sessions_root.rglob("rollout-*.jsonl")):
            if not jsonl.is_file():
                continue
            try:
                stat = jsonl.stat()
            except OSError:
                continue
            session_id = _session_id_from_filename(jsonl.name) or jsonl.stem
            yield SessionFile(
                source=self.name,
                path=jsonl,
                cwd=None,  # filled in lazily once iter_records sees session_meta
                session_id=session_id,
                started_at=None,
                last_modified=stat.st_mtime,
                extra={"codex_home": str(self.codex_home)},
            )

    # -- replay -----------------------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Stream ``(next_byte_offset, Record)`` pairs for ``session``.

        Stateful: tracks the most-recent ``turn_context.model`` and the
        ``session_meta.cwd``, threading both into every yielded Record so
        downstream code can rely on the same ``model`` / ``cwd`` field
        semantics as Claude Code sessions.

        Compaction emits a boundary marker followed by replayed
        ``replacement_history`` items (see ``_translate_compacted``).
        """
        state = _ReplayState()
        # Seed session_id from the filename so the *very first* line (which
        # arrives before session_meta has been parsed) is still attributable.
        state.session_id = _session_id_from_filename(session.path.name) or session.session_id
        if session.cwd:
            state.cwd = session.cwd

        path = Path(session.path)
        with path.open("rb") as f:
            if start_offset:
                f.seek(start_offset)
            offset = start_offset
            while True:
                line = f.readline()
                if not line:
                    break
                line_len = len(line)
                offset += line_len
                stripped = line.strip()
                if not stripped:
                    continue
                if not line.endswith(b"\n"):
                    # Truncated tail — the writer will rewrite; skip.
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                t = obj.get("type", "")
                payload = obj.get("payload", {})
                line_start = offset - line_len

                yielded_any = False
                for rec in _route(obj, t, payload, state, line_start):
                    yielded_any = True
                    yield offset, rec
                if not yielded_any:
                    # Unknown top-level type — preserve as base Record so
                    # offsets stay aligned with the file.
                    yield offset, Record(**state.base(obj, line_start))


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def _route(
    obj: dict[str, Any],
    t: str,
    payload: Any,
    state: _ReplayState,
    byte_offset: int,
) -> Iterator[Record]:
    """Top-level dispatch over the five RolloutItem variants."""
    if t == "session_meta" and isinstance(payload, dict):
        yield _translate_session_meta(obj, payload, state, byte_offset)
        return
    if t == "turn_context" and isinstance(payload, dict):
        yield _translate_turn_context(obj, payload, state, byte_offset)
        return
    if t == "event_msg" and isinstance(payload, dict):
        yield _translate_event_msg(obj, payload, state, byte_offset)
        return
    if t == "compacted" and isinstance(payload, dict):
        yield from _translate_compacted(obj, payload, state, byte_offset)
        return
    if t == "response_item":
        rec = _translate_response_item(obj, payload, state, byte_offset)
        if rec is not None:
            yield rec
        return


# ---------------------------------------------------------------------------
# filename parsing
# ---------------------------------------------------------------------------


def _session_id_from_filename(name: str) -> str | None:
    """Pull the thread UUID off the tail of ``rollout-<ts>-<uuid>.jsonl``.

    Returns ``None`` if the name doesn't match the expected pattern. UUIDs
    are 36 chars (``8-4-4-4-12`` with dashes); we use the last 36 chars
    of the stem so we don't have to parse the timestamp.
    """
    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
        return None
    stem = name[len("rollout-") : -len(".jsonl")]
    # stem = "YYYY-MM-DDThh-mm-ss-<uuid>"
    if len(stem) < 36:
        return None
    return stem[-36:]


# Register at import time.
SOURCES.append(CodexSource)


__all__ = [
    "CodexSource",
    "DEFAULT_CODEX_HOME",
]
