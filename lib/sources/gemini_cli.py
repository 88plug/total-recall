"""Gemini CLI adapter — `~/.gemini/tmp/<projectHash>/chats/session-*.jsonl`.

Gemini CLI persists chats as append-only JSONL, but with semantics that
differ materially from Claude Code's transcripts:

* The **first line** is a *session-metadata* row (``sessionId``,
  ``projectHash``, ``startTime``, ``lastUpdated``, ``directories``, ...).
  It is **not** a message.
* Subsequent lines are one of:

    1. ``MessageRecord``:
       ``{id, timestamp, type, content, displayContent, toolCalls?,
       thoughts?, tokens?, model?}`` — where ``type`` ∈
       ``user | gemini | info | error | warning``.

       Tool calls live inline on ``gemini`` messages in the ``toolCalls``
       array (one element per call). Gemini CLI **re-writes** the whole
       row on every tool-call status transition, so the same ``id`` can
       appear multiple times with different ``status`` values — last
       write wins (we yield the final state, not the intermediates).

    2. ``{"$set": {<partial>}}`` — apply to running session state
       (typically ``lastUpdated``, model swap, etc.).

    3. ``{"$rewindTo": "<messageId>"}`` — truncate the message log from
       (and including) that id forward. Used when the user edits an
       earlier turn and the conversation forks.

* Subagent transcripts live one directory deeper:
  ``~/.gemini/tmp/<projectHash>/chats/<parentSessionId>/<subagentSessionId>.jsonl``
  — discovered alongside the parent.

Because of the rewrite/rewind semantics, this adapter cannot stream
lazily by byte offset the way the Claude Code adapter does — it must
collapse the log into the *final* message sequence before yielding. The
``(cursor, Record)`` tuple still works: we yield a monotonically
increasing synthetic cursor (the index in the final replayed sequence),
which is what downstream checkpointing needs.

Content shape (``PartListUnion``)
---------------------------------

Gemini content is either a string or an array of typed parts:

* ``{text}``                         → text block
* ``{inlineData:{mimeType,data}}``   → multimodal; skipped (we do not
                                       index binary blobs)
* ``{functionCall:{name, args, id}}`` → translated to a
                                       :class:`Block` ``type="tool_use"``
                                       and surfaced on the assistant
                                       record (mirrors Anthropic shape)
* ``{functionResponse:{id, name, response}}`` → translated to a
                                       :class:`ToolResult` and surfaced
                                       on a synthetic ``user`` record
                                       (mirrors how Anthropic packs tool
                                       results as ``role:"user"``)

Token schema mapping
--------------------

Gemini's ``{input, output, cached, thoughts, tool, total}`` does not
align 1:1 with Anthropic's. We do best-fit:

* ``input``    → ``input_tokens``
* ``output``   → ``output_tokens``
* ``cached``   → ``cache_read_tokens``
* ``thoughts`` → ``cache_creation_tokens`` (best fit; "extended thinking"
                 has the closest semantics)
* ``tool``     → no direct mapping; preserved under
                 ``extras.gemini_tool_tokens`` so nothing is silently lost
* ``total``    → ``extras.gemini_total_tokens`` (we don't trust an upstream
                 sum)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
)
from lib.sources.base import SOURCES, SessionFile, SessionSource


# ---------------------------------------------------------------------------
# Helpers — parts / tokens / timestamps
# ---------------------------------------------------------------------------


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Best-effort ISO-8601 → UTC datetime (silently returns ``None``)."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _ts_seconds(raw: Any) -> Optional[float]:
    dt = _parse_ts(raw)
    return dt.timestamp() if dt is not None else None


def _flatten_content(content: Any) -> tuple[str, list[dict], list[dict]]:
    """Extract ``(text, function_calls, function_responses)`` from PartListUnion.

    Multimodal ``inlineData`` parts are dropped — we do not index binary
    payloads. Unknown part types are also dropped silently.
    """
    text_parts: list[str] = []
    fn_calls: list[dict] = []
    fn_resps: list[dict] = []
    if isinstance(content, str):
        return content, fn_calls, fn_resps
    if not isinstance(content, list):
        return "", fn_calls, fn_resps
    for part in content:
        if not isinstance(part, dict):
            continue
        if "text" in part and isinstance(part["text"], str):
            text_parts.append(part["text"])
        elif "functionCall" in part and isinstance(part["functionCall"], dict):
            fn_calls.append(part["functionCall"])
        elif "functionResponse" in part and isinstance(part["functionResponse"], dict):
            fn_resps.append(part["functionResponse"])
        # inlineData and unknown shapes intentionally skipped
    return "\n".join(text_parts), fn_calls, fn_resps


def _map_tokens(tokens: Any) -> dict[str, Any]:
    """Remap Gemini token schema → Anthropic-shaped usage dict.

    Returns an empty dict if ``tokens`` is missing / malformed.
    """
    if not isinstance(tokens, dict):
        return {}
    out: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    if "input" in tokens:
        out["input_tokens"] = tokens["input"]
    if "output" in tokens:
        out["output_tokens"] = tokens["output"]
    if "cached" in tokens:
        out["cache_read_tokens"] = tokens["cached"]
    if "thoughts" in tokens:
        out["cache_creation_tokens"] = tokens["thoughts"]
    if "tool" in tokens:
        extras["gemini_tool_tokens"] = tokens["tool"]
    if "total" in tokens:
        extras["gemini_total_tokens"] = tokens["total"]
    if extras:
        out["extras"] = extras
    return out


def _tool_call_to_block(tc: dict) -> Block:
    """Translate a Gemini ``toolCalls[]`` entry into a canonical tool_use Block.

    Gemini's tool-call shape is roughly
    ``{id, name, args, status, result?, error?}``. We surface ``id``/
    ``name``/``args`` on the :class:`ToolUseRef` and keep the full raw
    object (including ``status`` / ``result``) on ``Block.raw`` so the
    extractor pipeline can inspect outcome state.
    """
    return Block(
        type="tool_use",
        tool_use=ToolUseRef(
            id=str(tc.get("id", "") or ""),
            name=str(tc.get("name", "") or ""),
            input=tc.get("args") or {},
        ),
        raw=tc,
    )


def _function_call_to_block(fc: dict) -> Block:
    """Translate an inline ``functionCall`` content-part into a tool_use Block."""
    return Block(
        type="tool_use",
        tool_use=ToolUseRef(
            id=str(fc.get("id", "") or ""),
            name=str(fc.get("name", "") or ""),
            input=fc.get("args") or {},
        ),
        raw=fc,
    )


def _function_response_to_tool_result(fr: dict) -> ToolResult:
    """Translate an inline ``functionResponse`` content-part into a ToolResult.

    Gemini's response payload is free-form (``response`` is usually a dict
    or a string); we flatten to a string for ``content`` and keep the
    original under ``raw_content`` for any extractor that needs structure.
    """
    resp = fr.get("response")
    if isinstance(resp, str):
        text = resp
    else:
        try:
            text = json.dumps(resp, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(resp)
    return ToolResult(
        tool_use_id=str(fr.get("id", "") or ""),
        is_error=bool(fr.get("error")),
        content=text,
        raw_content=resp,
    )


# ---------------------------------------------------------------------------
# Replay — collapse the JSONL into the final canonical message sequence
# ---------------------------------------------------------------------------


def _replay(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a Gemini JSONL transcript and return ``(metadata, messages)``.

    * ``metadata`` is the running session-state dict (first-line seed
      plus all ``$set`` patches applied in order).
    * ``messages`` is the final list of message records, with:
        - duplicates collapsed last-write-wins (per ``id``)
        - ``$rewindTo`` truncations applied
        - chronological order preserved (by first-seen position)
    """
    metadata: dict[str, Any] = {}
    # We need both ``order`` (first time we saw the id, defines position)
    # and ``state`` (latest version of the record, last-write-wins).
    order: list[str] = []
    state: dict[str, dict[str, Any]] = {}

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return metadata, []

    with fh:
        first = True
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            # First non-blank line is session-metadata seed *iff* it lacks
            # an ``id`` (messages always have one). Defensive: also accept
            # the first line as a message if it looks like one.
            if first:
                first = False
                if "id" not in obj and "$set" not in obj and "$rewindTo" not in obj:
                    metadata.update(obj)
                    continue

            if "$set" in obj and isinstance(obj["$set"], dict):
                metadata.update(obj["$set"])
                continue

            if "$rewindTo" in obj:
                target = obj["$rewindTo"]
                if target in order:
                    idx = order.index(target)
                    for mid in order[idx:]:
                        state.pop(mid, None)
                    order = order[:idx]
                continue

            mid = obj.get("id")
            if not mid:
                # Anonymous record — keep but synthesize an id so dedupe
                # still works deterministically.
                mid = f"__anon_{len(order)}"
                obj = {**obj, "id": mid}
            if mid in state:
                # Re-write: replace contents but keep original position.
                state[mid] = obj
            else:
                order.append(mid)
                state[mid] = obj

    return metadata, [state[mid] for mid in order]


# ---------------------------------------------------------------------------
# Translation — Gemini message → canonical lib.schema.Record
# ---------------------------------------------------------------------------


def _translate(
    msg: dict[str, Any],
    metadata: dict[str, Any],
    cwd: Optional[str],
) -> Optional[Record]:
    """Translate one Gemini ``MessageRecord`` into a :class:`Record` subclass.

    Returns ``None`` for messages we have no mapping for (should not
    happen for known ``type`` values).
    """
    mtype = msg.get("type")
    session_id = metadata.get("sessionId")
    ts = _parse_ts(msg.get("timestamp"))
    uid = msg.get("id")
    text, fn_calls, fn_resps = _flatten_content(msg.get("content"))

    base: dict[str, Any] = dict(
        type=mtype if isinstance(mtype, str) else "?",
        uuid=uid,
        parent_uuid=None,  # Gemini does not encode parent ids
        session_id=session_id,
        ts=ts,
        cwd=cwd,
        git_branch=None,
        version=metadata.get("cliVersion") or metadata.get("version"),
        is_sidechain=bool(metadata.get("__is_sidechain")),
        raw=msg,
        byte_offset=0,
    )

    if mtype == "user":
        # A user message that contains only functionResponse parts is
        # really a tool-result envelope (mirrors Anthropic's user-role
        # tool_result packing). Surface accordingly.
        if fn_resps and not text and not fn_calls:
            base["type"] = "user"
            results = [_function_response_to_tool_result(fr) for fr in fn_resps]
            return UserRecord(
                **base,
                content_kind="tool_result",
                text=None,
                tool_results=results,
                tool_use_result_payload=None,
            )
        base["type"] = "user"
        return UserRecord(
            **base,
            content_kind="string" if text else "empty",
            text=text or None,
            tool_results=[],
            tool_use_result_payload=None,
        )

    if mtype == "gemini":
        blocks: list[Block] = []
        thoughts = msg.get("thoughts")
        if isinstance(thoughts, str) and thoughts:
            blocks.append(Block(type="thinking", thinking=thoughts, raw={"thinking": thoughts}))
        if text:
            blocks.append(Block(type="text", text=text, raw={"text": text}))
        for fc in fn_calls:
            blocks.append(_function_call_to_block(fc))
        # toolCalls[] takes precedence over functionCall content parts —
        # Gemini emits them as separate fields when the harness tracks
        # status transitions; if both appear, dedupe by id.
        seen_ids = {b.tool_use.id for b in blocks if b.type == "tool_use" and b.tool_use}
        for tc in msg.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            tid = str(tc.get("id", "") or "")
            if tid and tid in seen_ids:
                # Replace the earlier block with the (presumably more
                # complete) toolCalls entry — preserves "last status wins".
                blocks = [
                    b
                    for b in blocks
                    if not (b.type == "tool_use" and b.tool_use and b.tool_use.id == tid)
                ]
            blocks.append(_tool_call_to_block(tc))
            if tid:
                seen_ids.add(tid)

        base["type"] = "assistant"
        return AssistantRecord(
            **base,
            model=msg.get("model") or metadata.get("model"),
            content=blocks,
            usage=_map_tokens(msg.get("tokens")) or None,
            stop_reason=None,
            message_id=uid,
            request_id=None,
        )

    if mtype in {"info", "error", "warning"}:
        base["type"] = "system"
        payload = dict(msg)
        return SystemRecord(**base, subtype=mtype, payload=payload)

    # Unknown type — preserve as a generic Record so callers can still
    # see it (the schema's policy is "additive, never raise").
    return Record(**base)


# ---------------------------------------------------------------------------
# Discovery — find session and subagent .jsonl files on disk
# ---------------------------------------------------------------------------


def _extract_first_object(path: Path) -> Optional[dict]:
    """Return the first decoded JSON object in ``path``, or ``None``."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    except OSError:
        return None
    return None


def _seed_from_first_line(path: Path) -> dict[str, Any]:
    """First-line peek to surface ``sessionId``/``startTime``/``directories``.

    Cheap (one ``read`` of one line). Used by ``discover_sessions`` to
    populate :class:`SessionFile` without replaying the whole transcript.
    """
    obj = _extract_first_object(path)
    if not isinstance(obj, dict):
        return {}
    # A message-shaped first line still gives us sessionId via the
    # filename; pull whatever metadata-like keys are present.
    return {
        k: obj[k]
        for k in ("sessionId", "projectHash", "startTime", "directories", "cliVersion", "model")
        if k in obj
    }


def _cwd_from_seed(seed: dict[str, Any]) -> Optional[str]:
    """Recover ``cwd`` from the seed's ``directories`` array if present."""
    dirs = seed.get("directories")
    if isinstance(dirs, list) and dirs:
        first = dirs[0]
        if isinstance(first, str) and first:
            return first
    return None


class GeminiCliSource(SessionSource):
    """Adapter for Gemini CLI's ``~/.gemini/tmp/<projectHash>/chats/`` layout.

    The projects-root is overridable for tests; production code should
    construct with no arguments and accept ``~/.gemini/tmp``.
    """

    name = "gemini_cli"

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root if root is not None else Path.home() / ".gemini" / "tmp"

    def is_available(self) -> bool:
        """``True`` iff ``~/.gemini/tmp`` exists *and* contains at least one
        entry. A single ``stat()`` + ``iterdir()`` peek — no file bodies.
        """
        if not self.root.exists():
            return False
        try:
            next(iter(self.root.iterdir()))
        except (StopIteration, OSError):
            return False
        return True

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield a :class:`SessionFile` per ``session-*.jsonl`` (and per
        subagent file under ``<parentSessionId>/``).

        Order is project-hash → chronological filename, both lexical, so
        callers can checkpoint deterministically.
        """
        if not self.is_available():
            return
        for project_hash_dir in sorted(self.root.iterdir()):
            if not project_hash_dir.is_dir():
                continue
            chats_dir = project_hash_dir / "chats"
            if not chats_dir.is_dir():
                continue
            project_hash = project_hash_dir.name

            for jsonl in sorted(chats_dir.glob("session-*.jsonl")):
                if not jsonl.is_file():
                    continue
                try:
                    stat = jsonl.stat()
                except OSError:
                    continue
                seed = _seed_from_first_line(jsonl)
                yield SessionFile(
                    source=self.name,
                    path=jsonl,
                    cwd=_cwd_from_seed(seed),
                    session_id=str(seed.get("sessionId") or jsonl.stem),
                    started_at=_ts_seconds(seed.get("startTime")),
                    last_modified=stat.st_mtime,
                    extra={
                        "projectHash": project_hash,
                        "seed": seed,
                        "is_subagent": False,
                    },
                )

            # Subagent transcripts live under
            # ``<chats>/<parentSessionId>/<subagentSessionId>.jsonl``.
            for sub_dir in sorted(p for p in chats_dir.iterdir() if p.is_dir()):
                for jsonl in sorted(sub_dir.glob("*.jsonl")):
                    if not jsonl.is_file():
                        continue
                    try:
                        stat = jsonl.stat()
                    except OSError:
                        continue
                    seed = _seed_from_first_line(jsonl)
                    yield SessionFile(
                        source=self.name,
                        path=jsonl,
                        cwd=_cwd_from_seed(seed),
                        session_id=str(seed.get("sessionId") or jsonl.stem),
                        started_at=_ts_seconds(seed.get("startTime")),
                        last_modified=stat.st_mtime,
                        extra={
                            "projectHash": project_hash,
                            "parent_session_id": sub_dir.name,
                            "seed": seed,
                            "is_subagent": True,
                        },
                    )

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Replay-and-yield: collapse the JSONL into its final message
        sequence, translate each survivor to :class:`Record`, and yield
        ``(cursor, Record)`` with ``cursor`` a 1-based replayed-position
        index.

        The cursor is **not** a byte offset — Gemini's rewrite/rewind
        semantics make byte offsets meaningless for resumption. Callers
        that checkpoint should treat ``start_offset`` as a count of
        already-yielded records to skip.
        """
        metadata, messages = _replay(session.path)
        # Subagent flag is propagated from discovery into the metadata so
        # the translator can stamp ``is_sidechain``.
        if session.extra.get("is_subagent"):
            metadata = {**metadata, "__is_sidechain": True}
        cwd = session.cwd or _cwd_from_seed(metadata)

        for i, msg in enumerate(messages, start=1):
            if i <= start_offset:
                continue
            rec = _translate(msg, metadata, cwd)
            if rec is None:
                continue
            yield i, rec


# Register at import time — :func:`lib.sources.all_sources` walks SOURCES.
SOURCES.append(GeminiCliSource)


__all__ = ["GeminiCliSource"]
