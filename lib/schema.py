"""Typed dataclasses for every Claude Code session-transcript record type.

The schema is empirically derived from a real corpus on this machine (~92
sessions, 19 projects, 776 MB total). Record-type counts at the top of the
``parse_record`` docstring reflect that sample; treat field presence as
*additive* — newer Claude Code versions (we see 2.1.138 → 2.1.150) only ever
add keys, so the parser tolerates unknown fields by stashing the full raw
object in :attr:`Record.raw`.

Discriminators
==============

* Top-level ``type`` selects the dataclass.
* For ``attachment`` records the inner discriminator is **nested** at
  ``attachment.type`` — *not* a top-level ``subtype`` (a real gotcha; see
  the project notes).
* For ``system`` records the inner discriminator is top-level ``subtype``.
* For ``user`` records the *content shape* is polymorphic, so
  :class:`UserRecord` exposes a normalized ``content_kind`` literal plus
  pre-extracted ``text`` / ``tool_results`` / ``tool_use_result_payload``.

The first line of a session has ``cwd``/``gitBranch``/``version`` = ``None``
(usually a ``permission-mode`` row). Do not rely on those keys from line 1;
walk forward or derive ``cwd`` from the directory slug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ContentKind = Literal["string", "tool_result", "text_array", "image", "document", "empty"]


# ---------------------------------------------------------------------------
# Inner block types (assistant.message.content[] elements + user tool_results)
# ---------------------------------------------------------------------------


@dataclass
class ToolUseRef:
    """A ``tool_use`` block inside an assistant message."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Block:
    """A normalized assistant content block.

    The raw shape is one of ``text`` | ``thinking`` | ``tool_use``. Fields
    not relevant for the block's type are ``None``. Other unknown block
    types are preserved with ``type`` set and everything else ``None``.
    """

    type: str
    text: str | None = None
    thinking: str | None = None
    thinking_signature: str | None = None
    tool_use: ToolUseRef | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """A ``tool_result`` block inside a user message.

    ``content`` is normalized to a string when the source is a list of
    ``text`` blocks; complex inner structures (lists with ``tool_reference``
    blocks etc.) keep their original shape in ``raw_content``.
    """

    tool_use_id: str
    is_error: bool
    content: str
    raw_content: Any = None


# ---------------------------------------------------------------------------
# Record base + subclasses
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """Base for every parsed session-transcript record.

    Cross-cutting keys observed on most records are surfaced; everything
    else lives in :attr:`raw`. ``byte_offset`` is the *start* offset of the
    line in the source file (handy for incremental tail readers).
    """

    type: str
    uuid: str | None
    parent_uuid: str | None
    session_id: str | None
    ts: datetime | None
    cwd: str | None
    git_branch: str | None
    version: str | None
    is_sidechain: bool
    raw: dict[str, Any]
    byte_offset: int = 0


@dataclass
class AssistantRecord(Record):
    """A model turn (``type:"assistant"``).

    ``content`` blocks are normalized into :class:`Block`. Tool calls show
    up as ``Block(type="tool_use", tool_use=ToolUseRef(...))``. ``thinking``
    blocks include their ``signature`` (Anthropic anti-tamper field) — those
    are private; do not surface raw thinking text back to the user.
    """

    model: str | None = None
    content: list[Block] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    message_id: str | None = None
    request_id: str | None = None


@dataclass
class UserRecord(Record):
    """A user-role turn — polymorphic content.

    Anthropic packs tool results as ``role:"user"`` envelopes, so this
    record covers both real prompts and tool results. ``content_kind``
    discriminates:

    * ``"string"`` — real human prompt, or a system interrupt like
      ``"[Request interrupted by user]"``, or a ``isCompactSummary`` body.
    * ``"tool_result"`` — one or more ``tool_result`` blocks. The parent
      ``toolUseResult`` payload (Bash stdout/stderr, Edit structuredPatch,
      TaskCreate agentId/status/usage, etc.) is in
      :attr:`tool_use_result_payload`.
    * ``"text_array"`` — typed user text (often ``isMeta:true``).
    * ``"image"`` / ``"document"`` — rare.
    """

    content_kind: ContentKind = "empty"
    text: str | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    tool_use_result_payload: dict[str, Any] | None = None
    is_compact_summary: bool = False
    is_meta: bool = False


@dataclass
class SystemRecord(Record):
    """A ``type:"system"`` record. ``subtype`` is top-level.

    Observed subtypes: ``turn_duration``, ``api_error``, ``away_summary``
    (HIGH-VALUE narrative), ``local_command``, ``stop_hook_summary``,
    ``compact_boundary`` (with ``compactMetadata.preservedSegment``),
    ``scheduled_task_fire``.
    """

    subtype: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachmentRecord(Record):
    """An ``attachment`` record. Discriminator is **nested** at
    ``attachment.type``.

    Inner types observed: ``skill_listing``, ``deferred_tools_delta``,
    ``task_reminder``, ``queued_command``, ``edited_text_file``,
    ``mcp_instructions_delta``, ``file``, ``date_change``,
    ``compact_file_reference``, ``nested_memory``, ``hook_system_message``,
    ``hook_success``, ``hook_additional_context``, ``command_permissions``,
    ``plan_mode_exit``, ``task_status``, ``plan_mode``,
    ``max_turns_reached``, ``invoked_skills``, ``goal_status``.
    """

    attachment_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueueOperationRecord(Record):
    """User input queued mid-turn (``operation: enqueue/...``)."""

    operation: str = ""
    content: str | None = None


@dataclass
class AiTitleRecord(Record):
    """Auto-generated session title. Cheap session-level label."""

    ai_title: str = ""


@dataclass
class LastPromptRecord(Record):
    """Most recent user prompt + leaf-uuid pointer. Resume marker."""

    last_prompt: str = ""
    leaf_uuid: str | None = None


@dataclass
class PermissionModeRecord(Record):
    """Permission mode set at session start (``bypassPermissions``, etc.)."""

    permission_mode: str = ""


@dataclass
class FileHistorySnapshotRecord(Record):
    """File-history feature backup pointer."""

    message_id: str | None = None
    is_snapshot_update: bool = False
    snapshot: Any = None


@dataclass
class AgentNameRecord(Record):
    """Session-scoped agent-name label (``type:"agent-name"``).

    Empirically a session-metadata row with shape
    ``{type, agentName, sessionId}`` (no ``uuid``/``parentUuid``/``timestamp``).
    Observed in ``~/.claude/projects/-home-operator-proxmox-plus/`` where a
    sub-agent run carries its name forward (e.g. ``agentName:
    "google-surveillance-blocking"``). Like ``ai-title`` and ``last-prompt``,
    it is metadata — not a conversation turn — and therefore deliberately
    excluded from the parent-uuid DAG in :mod:`lib.dag`.
    """

    agent_name: str = ""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        # 2026-05-25T06:12:31.998Z
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _base_kwargs(obj: dict[str, Any], byte_offset: int) -> dict[str, Any]:
    return dict(
        type=obj.get("type", "?"),
        uuid=obj.get("uuid"),
        parent_uuid=obj.get("parentUuid"),
        session_id=obj.get("sessionId"),
        ts=_parse_ts(obj.get("timestamp")),
        cwd=obj.get("cwd"),
        git_branch=obj.get("gitBranch"),
        version=obj.get("version"),
        is_sidechain=bool(obj.get("isSidechain", False)),
        raw=obj,
        byte_offset=byte_offset,
    )


def _parse_assistant_block(b: Any) -> Block:
    if not isinstance(b, dict):
        return Block(type="?", raw={"value": b})
    bt = b.get("type", "?")
    if bt == "text":
        return Block(type=bt, text=b.get("text", ""), raw=b)
    if bt == "thinking":
        return Block(
            type=bt,
            thinking=b.get("thinking", ""),
            thinking_signature=b.get("signature"),
            raw=b,
        )
    if bt == "tool_use":
        return Block(
            type=bt,
            tool_use=ToolUseRef(
                id=b.get("id", ""),
                name=b.get("name", ""),
                input=b.get("input", {}) or {},
            ),
            raw=b,
        )
    return Block(type=bt, raw=b)


def _classify_user_content(content: Any) -> ContentKind:
    if isinstance(content, str):
        return "string"
    if not isinstance(content, list):
        return "empty"
    if not content:
        return "empty"
    # Inspect first block; in practice user.content lists are homogeneous in kind.
    first = content[0]
    if not isinstance(first, dict):
        return "empty"
    ft = first.get("type")
    if ft == "tool_result":
        return "tool_result"
    if ft == "text":
        return "text_array"
    if ft == "image":
        return "image"
    if ft == "document":
        return "document"
    return "empty"


def _normalize_tool_result_content(c: Any) -> tuple[str, Any]:
    """Best-effort flatten to a string; keep original in ``raw_content``."""
    if isinstance(c, str):
        return c, c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts), c
    return "", c


def _extract_user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(p for p in parts if p) or None
    return None


def _extract_tool_results(content: Any) -> list[ToolResult]:
    out: list[ToolResult] = []
    if not isinstance(content, list):
        return out
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        text, raw_c = _normalize_tool_result_content(b.get("content", ""))
        out.append(
            ToolResult(
                tool_use_id=b.get("tool_use_id", ""),
                is_error=bool(b.get("is_error", False)),
                content=text,
                raw_content=raw_c,
            )
        )
    return out


def parse_record(obj: dict[str, Any], byte_offset: int = 0) -> Record:
    """Parse one decoded JSON object into the appropriate Record subclass.

    Unknown ``type`` values fall through to the base :class:`Record` so the
    walker never raises on additive schema changes.
    """

    base = _base_kwargs(obj, byte_offset)
    t = base["type"]

    if t == "assistant":
        msg = obj.get("message") or {}
        content_raw = msg.get("content", [])
        blocks = (
            [_parse_assistant_block(b) for b in content_raw]
            if isinstance(content_raw, list)
            else []
        )
        return AssistantRecord(
            **base,
            model=msg.get("model"),
            content=blocks,
            usage=msg.get("usage"),
            stop_reason=msg.get("stop_reason"),
            message_id=msg.get("id"),
            request_id=obj.get("requestId"),
        )

    if t == "user":
        msg = obj.get("message") or {}
        content = msg.get("content", "")
        kind = _classify_user_content(content)
        text = _extract_user_text(content)
        results = _extract_tool_results(content) if kind == "tool_result" else []
        tur = obj.get("toolUseResult")
        return UserRecord(
            **base,
            content_kind=kind,
            text=text,
            tool_results=results,
            tool_use_result_payload=tur if isinstance(tur, dict) else None,
            is_compact_summary=bool(obj.get("isCompactSummary", False)),
            is_meta=bool(obj.get("isMeta", False)),
        )

    if t == "system":
        subtype = obj.get("subtype", "") or ""
        payload = {k: v for k, v in obj.items() if k not in {"type", "subtype"}}
        return SystemRecord(**base, subtype=subtype, payload=payload)

    if t == "attachment":
        att = obj.get("attachment") or {}
        att_type = att.get("type", "") if isinstance(att, dict) else ""
        return AttachmentRecord(
            **base,
            attachment_type=att_type or "",
            payload=att if isinstance(att, dict) else {},
        )

    if t == "queue-operation":
        return QueueOperationRecord(
            **base,
            operation=obj.get("operation", "") or "",
            content=obj.get("content"),
        )

    if t == "ai-title":
        return AiTitleRecord(**base, ai_title=obj.get("aiTitle", "") or "")

    if t == "last-prompt":
        return LastPromptRecord(
            **base,
            last_prompt=obj.get("lastPrompt", "") or "",
            leaf_uuid=obj.get("leafUuid"),
        )

    if t == "permission-mode":
        return PermissionModeRecord(**base, permission_mode=obj.get("permissionMode", "") or "")

    if t == "file-history-snapshot":
        return FileHistorySnapshotRecord(
            **base,
            message_id=obj.get("messageId"),
            is_snapshot_update=bool(obj.get("isSnapshotUpdate", False)),
            snapshot=obj.get("snapshot"),
        )

    if t == "agent-name":
        return AgentNameRecord(**base, agent_name=obj.get("agentName", "") or "")

    return Record(**base)
