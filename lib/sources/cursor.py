"""Cursor adapter — JSONL agent-transcripts (v1) + vscdb SQLite (v2).

Scope
=====

v1 (JSONL)
----------
Reads ``~/.cursor/projects/<projHash>/agent-transcripts/*.jsonl`` — the
newer Cursor CLI era tree (~70% of sessions).

v2 (vscdb SQLite) — added here
-------------------------------
Reads ``state.vscdb`` files from:

* Global storage:
  - macOS: ``~/Library/Application Support/Cursor/User/globalStorage/``
  - Linux: ``~/.config/Cursor/User/globalStorage/``
  - Windows: ``%APPDATA%\\Cursor\\User\\globalStorage\\``
  - Remote-SSH: ``~/.cursor-server/data/User/globalStorage/``
* Workspace storage: sibling ``workspaceStorage/<sha>/`` dirs

This covers the ~30% of Cursor sessions that live exclusively in the
IDE-side SQLite store (older installs, non-JSONL flows).

Tables
------

``cursorDiskKV`` (modern, global DB only):
  Key prefixes:
  - ``composerData:<composerId>`` — session metadata (name, timestamps,
    ``fullConversationHeadersOnly``, token usage)
  - ``bubbleId:<composerId>:<bubbleId>`` — individual messages (largest
    table). Bubble ``type`` field: 1=user, 2=assistant.
  - ``agentKv`` — request-level archive (skipped for now)
  - ``messageRequestContext:<composerId>`` — per-session context

  Text content fields (in priority order):
  1. ``text`` (assistant) / ``content`` / ``finalText`` / ``message``
  2. ``codeBlocks[].content`` — code artifacts
  3. ``toolFormerData.result`` — tool call result
  Timestamps: ``createdAt`` (ISO, new format) → ``timingInfo.clientRpcSendTime``
  → ``timingInfo.clientEndTime`` (Unix ms).
  Role: ``type == 2`` → assistant, anything else → user.
  Model: ``modelInfo.modelName``.
  Usage: ``tokenCount.inputTokens/outputTokens`` (camelCase primary) →
         ``usage.input_tokens/output_tokens`` (snake_case fallback).

``ItemTable`` (legacy, workspace DBs):
  Key ``workbench.panel.aichat.view.aichat.chatdata`` → JSON blob with
  ``{tabs: [{bubbles: [{...}]}]}`` structure (older Cursor versions).
  Also ``composer.composerData`` → ``{allComposers: [{composerId, name, ...}]}``
  with messages in ``messages``/``bubbles`` sub-arrays.

workspace.json resolution
-------------------------
Each ``workspaceStorage/<sha>/`` dir contains a sibling ``workspace.json``
with ``{"folder": "file:///path/to/repo"}``. This is the only offline
source for recovering cwd. If absent, ``cwd=None`` and
``extra["unresolved_cwd"] = True``.

JSONL line shape (unchanged from v1)
-------------------------------------
Each line: ``{id, timestamp, role, content, model, usage, ...}``.
Schema drifts — parser is deliberately tolerant.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import sys
import urllib.parse
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    ToolResult,
    UserRecord,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_cursor_ts(raw: Any) -> datetime | None:
    """Parse Cursor's timestamp field — both ISO-8601 and epoch seen.

    Returns ``None`` on anything that isn't recognisable; never raises.
    """

    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None
    return None


def _extract_text(content: Any) -> str | None:
    """Flatten Cursor content to plain text where possible.

    Cursor content is either a string or a list of ``{type, text}`` blocks
    (same shape as Anthropic). Anything else returns ``None``.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts) if parts else None
    return None


def _assistant_blocks(content: Any) -> list[Block]:
    """Normalize Cursor assistant content into ``lib.schema.Block`` list."""

    if isinstance(content, str):
        return [Block(type="text", text=content, raw={"type": "text", "text": content})]
    if not isinstance(content, list):
        return []
    out: list[Block] = []
    for b in content:
        if not isinstance(b, dict):
            out.append(Block(type="?", raw={"value": b}))
            continue
        bt = b.get("type", "?")
        if bt == "text":
            out.append(Block(type=bt, text=b.get("text", ""), raw=b))
        else:
            # Unknown / tool_use / image — preserve raw, type-only Block.
            out.append(Block(type=bt, raw=b))
    return out


def _cursor_line_to_record(
    obj: dict[str, Any], session: SessionFile, byte_offset: int = 0
) -> Record:
    """Translate one decoded Cursor JSONL line into a canonical Record.

    The translator is intentionally tolerant: unknown roles fall through
    to the base :class:`Record` rather than raising. The original object
    is always preserved in :attr:`Record.raw` so downstream code can
    reach for fields the adapter does not yet surface.
    """

    role = obj.get("role")
    ts = _parse_cursor_ts(obj.get("timestamp"))
    uuid_ = obj.get("id")
    if uuid_ is not None and not isinstance(uuid_, str):
        uuid_ = str(uuid_)

    base: dict[str, Any] = dict(
        type=role if isinstance(role, str) else "?",
        uuid=uuid_,
        parent_uuid=None,
        session_id=session.session_id,
        ts=ts,
        cwd=session.cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw=obj,
        byte_offset=byte_offset,
    )

    content = obj.get("content")

    if role == "assistant":
        return AssistantRecord(
            **base,
            model=obj.get("model"),
            content=_assistant_blocks(content),
            usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else None,
            stop_reason=obj.get("stop_reason") or obj.get("stopReason"),
            message_id=uuid_,
            request_id=obj.get("requestId") or obj.get("request_id"),
        )

    if role == "user":
        text = _extract_text(content)
        return UserRecord(
            **base,
            content_kind="string"
            if isinstance(content, str)
            else ("text_array" if isinstance(content, list) and content else "empty"),
            text=text,
            tool_results=[],
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=False,
        )

    if role == "tool":
        # Cursor surfaces tool output as role=tool. Re-pack into a
        # UserRecord with a tool_result block so downstream extractors
        # that key off ``content_kind == "tool_result"`` keep working.
        tool_use_id = (
            obj.get("tool_call_id") or obj.get("toolCallId") or obj.get("tool_use_id") or ""
        )
        text = _extract_text(content) or ""
        is_error = bool(obj.get("is_error") or obj.get("isError") or False)
        tr = ToolResult(
            tool_use_id=str(tool_use_id),
            is_error=is_error,
            content=text,
            raw_content=content,
        )
        # type is preserved as "tool" so callers can distinguish if needed.
        return UserRecord(
            **base,
            content_kind="tool_result",
            text=None,
            tool_results=[tr],
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=False,
        )

    # Unknown / missing role — base Record, raw preserved.
    return Record(**base)


# ---------------------------------------------------------------------------
# vscdb helpers
# ---------------------------------------------------------------------------

# Minimum valid Unix-millisecond timestamp (Sep 9 2001) used by cursor-history
# to distinguish ms from seconds and reject junk values.
_MIN_VALID_UNIX_MS = 1_000_000_000_000


def _cursor_user_bases() -> list[Path]:
    """Return platform-specific ``Cursor/User`` base paths to search.

    Separated out so tests can mock this function directly without
    patching ``sys.platform`` and fighting ``Path.expanduser()``.
    """
    home = Path.home()
    if sys.platform == "darwin":
        primary = home / "Library" / "Application Support" / "Cursor" / "User"
    elif sys.platform == "win32":
        primary = (
            Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Cursor" / "User"
        )
    else:
        primary = home / ".config" / "Cursor" / "User"

    remote = home / ".cursor-server" / "data" / "User"
    return [primary, remote]


def _discover_vscdb_paths() -> list[Path]:
    """Return all detected ``state.vscdb`` paths (global + workspaces).

    Covers macOS, Linux, Windows and Remote-SSH layouts. Skips paths
    that don't exist on the current machine.
    """
    candidates: list[Path] = []

    for root in _cursor_user_bases():
        global_db = root / "globalStorage" / "state.vscdb"
        if global_db.exists():
            candidates.append(global_db)
        ws_root = root / "workspaceStorage"
        if ws_root.exists():
            try:
                for sha_dir in ws_root.iterdir():
                    vscdb = sha_dir / "state.vscdb"
                    if vscdb.exists():
                        candidates.append(vscdb)
            except OSError:
                pass

    return candidates


def _resolve_cwd_for_vscdb(vscdb: Path) -> str | None:
    """Read sibling ``workspace.json`` and return the decoded filesystem path.

    workspace.json shape (from thomas-pedersen/cursor-chat-browser):
      ``{"folder": "file:///absolute/path"}``  — single-folder workspace
      ``{"workspace": "file:///path/to/foo.code-workspace"}``  — .code-workspace

    Returns ``None`` if the file is absent or unreadable.
    """
    for _key in ("folder", "workspace"):
        ws_json = vscdb.parent / "workspace.json"
        if not ws_json.exists():
            return None
        try:
            data = json.loads(ws_json.read_text(encoding="utf-8", errors="replace"))
            for field_key in ("folder", "workspace"):
                folder = data.get(field_key) or ""
                if folder.startswith("file://"):
                    path_str = folder[len("file://") :]
                    return urllib.parse.unquote(path_str) or None
                if folder:
                    return folder
            return None
        except (json.JSONDecodeError, OSError):
            return None
    return None  # unreachable, but satisfies type-checker


def _parse_bubble_ts(data: dict[str, Any]) -> datetime | None:
    """Extract the best available timestamp from a raw bubble dict.

    Priority chain (from S2thend/cursor-history extractTimestamp):
    1. ``createdAt`` — ISO string (new Cursor format, >= 2025-09)
    2. ``timingInfo.clientRpcSendTime`` — Unix ms (old, assistant only)
    3. ``timingInfo.clientSettleTime`` — Unix ms (old, sometimes present)
    4. ``timingInfo.clientEndTime`` — Unix ms (old)
    5. None — caller fills gaps via interpolation if needed.
    """
    created = data.get("createdAt")
    if isinstance(created, str):
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass

    timing = data.get("timingInfo")
    if isinstance(timing, dict):
        for key in ("clientRpcSendTime", "clientSettleTime", "clientEndTime"):
            val = timing.get(key)
            if isinstance(val, (int, float)) and val > _MIN_VALID_UNIX_MS:
                try:
                    return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
                except (ValueError, OSError, OverflowError):
                    pass
    return None


def _extract_bubble_text(data: dict[str, Any]) -> str | None:
    """Extract plain-text content from a raw vscdb bubble dict.

    Mirrors the priority chain from S2thend/cursor-history extractBubbleText:
    assistant (type==2): ``text`` → ``toolFormerData.result`` → ``codeBlocks``
    user: ``codeBlocks`` → ``text`` / ``content`` / ``finalText`` / ``message``
    """
    is_assistant = data.get("type") == 2

    # Code blocks — used by both roles
    code_parts: list[str] = []
    for cb in data.get("codeBlocks") or []:
        if isinstance(cb, dict):
            c = cb.get("content")
            if isinstance(c, str) and c.strip():
                code_parts.append(c)

    if is_assistant:
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            return (text + "\n\n" + "\n\n".join(code_parts)).strip() if code_parts else text
        # tool result fallback
        tfd = data.get("toolFormerData")
        if isinstance(tfd, dict):
            result = tfd.get("result")
            if isinstance(result, str) and result.strip():
                return result
        if code_parts:
            return "\n\n".join(code_parts)
    else:
        if code_parts:
            return "\n\n".join(code_parts)

    # Common text fields for user messages and fallback
    for key in ("text", "content", "finalText", "message", "markdown", "textDescription"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val

    return None


def _bubble_to_record(
    bubble_data: dict[str, Any],
    composer_id: str,
    session: SessionFile,
    row_key: str,
) -> Record:
    """Translate one deserialized vscdb bubble dict into a canonical Record.

    Bubble type field (from S2thend/cursor-history):
      type == 2 → assistant
      anything else → user

    Tolerant: field access is wrapped so schema drift never raises.
    """
    bubble_type = bubble_data.get("type")
    is_assistant = bubble_type == 2
    role = "assistant" if is_assistant else "user"

    ts = _parse_bubble_ts(bubble_data)
    bubble_id = bubble_data.get("bubbleId") or row_key.split(":")[-1]
    if not isinstance(bubble_id, str):
        bubble_id = str(bubble_id)

    # Token usage: camelCase primary, snake_case fallback
    usage: dict[str, Any] | None = None
    token_count = bubble_data.get("tokenCount")
    if isinstance(token_count, dict):
        inp = token_count.get("inputTokens", 0) or 0
        out = token_count.get("outputTokens", 0) or 0
        if inp or out:
            usage = {"input_tokens": inp, "output_tokens": out}
    if usage is None:
        raw_usage = bubble_data.get("usage")
        if isinstance(raw_usage, dict):
            inp = raw_usage.get("input_tokens", 0) or 0
            out = raw_usage.get("output_tokens", 0) or 0
            if inp or out:
                usage = {"input_tokens": inp, "output_tokens": out}

    # Model name
    model: str | None = None
    model_info = bubble_data.get("modelInfo")
    if isinstance(model_info, dict):
        mn = model_info.get("modelName")
        if isinstance(mn, str) and mn.strip():
            model = mn

    base: dict[str, Any] = dict(
        type=role,
        uuid=bubble_id,
        parent_uuid=None,
        session_id=composer_id,
        ts=ts,
        cwd=session.cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw=bubble_data,
        byte_offset=0,
    )

    text = _extract_bubble_text(bubble_data)

    if is_assistant:
        blocks: list[Block] = []
        if text:
            blocks = [Block(type="text", text=text, raw={"type": "text", "text": text})]
        return AssistantRecord(
            **base,
            model=model,
            content=blocks,
            usage=usage,
            stop_reason=bubble_data.get("stopReason") or bubble_data.get("stop_reason"),
            message_id=bubble_id,
            request_id=bubble_data.get("requestId") or bubble_data.get("request_id"),
        )

    return UserRecord(
        **base,
        content_kind="string" if isinstance(text, str) else "empty",
        text=text,
        tool_results=[],
        tool_use_result_payload=None,
        is_compact_summary=False,
        is_meta=False,
    )


# Legacy ItemTable key families (from community tools, priority order)
_ITEM_TABLE_CHAT_KEYS = (
    "workbench.panel.aichat.view.aichat.chatdata",
    "workbench.panel.chat.view.chat.chatdata",
    "composer.composerData",
)


def _iter_item_table_records(
    conn: sqlite3.Connection,
    session: SessionFile,
) -> Iterator[tuple[int, Record]]:
    """Yield records from legacy ItemTable chat-data blob.

    Handles two blob shapes:
    * ``{tabs: [{bubbles: [{...}]}]}`` — oldest format
    * ``{allComposers: [{composerId, messages/bubbles, ...}]}`` — mid-era

    All field access is try/except tolerant.
    """
    data_str: str | None = None
    for key in _ITEM_TABLE_CHAT_KEYS:
        try:
            row = conn.execute("SELECT value FROM ItemTable WHERE [key] = ?", (key,)).fetchone()
            if row and row[0]:
                data_str = (
                    row[0] if isinstance(row[0], str) else row[0].decode("utf-8", errors="replace")
                )
                break
        except sqlite3.Error:
            continue

    if not data_str:
        return

    try:
        blob = json.loads(data_str)
    except (json.JSONDecodeError, TypeError):
        return

    if not isinstance(blob, dict):
        return

    # Shape 1: {tabs: [{bubbles: [...]}]} or {chatSessions: [...]}
    sessions_raw = blob.get("tabs") or blob.get("chatSessions") or []
    if sessions_raw and isinstance(sessions_raw, list):
        seq = 0
        for tab in sessions_raw:
            if not isinstance(tab, dict):
                continue
            tab_id = tab.get("id") or session.session_id
            bubbles = tab.get("bubbles") or tab.get("messages") or []
            for bubble in bubbles:
                if not isinstance(bubble, dict):
                    continue
                try:
                    rec = _bubble_to_record(bubble, str(tab_id), session, f"item:{seq}")
                    seq += 1
                    yield seq, rec
                except Exception:
                    seq += 1
        return

    # Shape 2: {allComposers: [{composerId, messages/bubbles, ...}]}
    all_composers = blob.get("allComposers") or []
    if all_composers and isinstance(all_composers, list):
        seq = 0
        for composer in all_composers:
            if not isinstance(composer, dict):
                continue
            cid = composer.get("composerId") or session.session_id
            msgs = (
                composer.get("messages")
                or composer.get("bubbles")
                or composer.get("conversation")
                or []
            )
            for bubble in msgs:
                if not isinstance(bubble, dict):
                    continue
                try:
                    rec = _bubble_to_record(bubble, str(cid), session, f"item:{seq}")
                    seq += 1
                    yield seq, rec
                except Exception:
                    seq += 1


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CursorSource(SessionSource):
    """Cursor adapter — JSONL agent-transcripts (v1) + vscdb SQLite (v2).

    The cursor home directory is overridable for tests.  Production code
    uses the platform default for JSONL.  vscdb paths are discovered
    independently via :func:`_discover_vscdb_paths`.
    """

    name = "cursor"

    def __init__(
        self,
        cursor_home: Path | None = None,
        vscdb_paths: list[Path] | None = None,
    ) -> None:
        self.cursor_home = cursor_home if cursor_home is not None else Path.home() / ".cursor"
        self.projects_root = self.cursor_home / "projects"
        # Allow explicit injection (tests); otherwise discover at construction.
        self._vscdb_paths: list[Path] = (
            vscdb_paths if vscdb_paths is not None else _discover_vscdb_paths()
        )

    # ------------------------------------------------------------------
    # is_available
    # ------------------------------------------------------------------

    def _jsonl_available(self) -> bool:
        """``True`` iff at least one project dir has an ``agent-transcripts/``.

        Cheap — bounded ``stat()`` walk; never opens a JSONL.
        """
        if not self.projects_root.is_dir():
            return False
        try:
            for p in self.projects_root.iterdir():
                if not p.is_dir():
                    continue
                if (p / "agent-transcripts").is_dir():
                    return True
        except OSError:
            return False
        return False

    def is_available(self) -> bool:
        """``True`` iff the JSONL tree *or* at least one vscdb exists."""
        return self._jsonl_available() or bool(self._vscdb_paths)

    # ------------------------------------------------------------------
    # discover_sessions
    # ------------------------------------------------------------------

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield JSONL sessions (v1) then vscdb sessions (v2)."""
        yield from self._discover_jsonl_sessions()
        for vscdb in self._vscdb_paths:
            yield from self._discover_vscdb_sessions(vscdb)

    def _discover_jsonl_sessions(self) -> Iterator[SessionFile]:
        """Yield a :class:`SessionFile` per ``.jsonl`` under every project.

        Lazy: no file body is read. Order is project-then-filename, both
        sorted lexicographically, so callers can checkpoint. Files whose
        ``stat()`` fails are silently skipped.

        ``cwd`` is set to the opaque ``projHash`` as a surrogate, with
        ``extra["unresolved_cwd"] = True`` to flag that a manual mapping
        is still required.
        """
        if not self.projects_root.is_dir():
            return
        for proj_dir in sorted(self.projects_root.iterdir()):
            if not proj_dir.is_dir():
                continue
            transcripts_dir = proj_dir / "agent-transcripts"
            if not transcripts_dir.is_dir():
                continue
            for jsonl in sorted(transcripts_dir.glob("*.jsonl")):
                if not jsonl.is_file():
                    continue
                try:
                    st = jsonl.stat()
                except OSError:
                    continue
                yield SessionFile(
                    source=self.name,
                    path=jsonl,
                    cwd=None,
                    session_id=jsonl.stem,
                    started_at=None,
                    last_modified=st.st_mtime,
                    extra={
                        "projHash": proj_dir.name,
                        "unresolved_cwd": True,
                    },
                )

    def _discover_vscdb_sessions(self, vscdb: Path) -> Iterator[SessionFile]:
        """Query ``cursorDiskKV`` for ``composerData:*`` keys → one :class:`SessionFile` each.

        Falls back to ``ItemTable`` when ``cursorDiskKV`` is absent (older
        workspace DBs).  ``cwd`` is recovered from sibling ``workspace.json``;
        absent → ``None`` with ``extra["unresolved_cwd"] = True``.
        """
        cwd = _resolve_cwd_for_vscdb(vscdb)
        try:
            conn = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            log.warning("cursor vscdb %s: cannot open: %s", vscdb, exc)
            return

        try:
            # Check if cursorDiskKV exists
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
            ).fetchone()

            if tbl:
                yield from self._yield_cursordiskkv_sessions(conn, vscdb, cwd)
            else:
                # Legacy workspace DB — yield one synthetic session for the whole file.
                yield from self._yield_item_table_sessions(conn, vscdb, cwd)

        except sqlite3.Error as exc:
            log.warning("cursor vscdb %s skipped: %s", vscdb, exc)
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _yield_cursordiskkv_sessions(
        self,
        conn: sqlite3.Connection,
        vscdb: Path,
        cwd: str | None,
    ) -> Iterator[SessionFile]:
        try:
            mtime = vscdb.stat().st_mtime
        except OSError:
            mtime = 0.0

        try:
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("cursor vscdb %s: cursorDiskKV query failed: %s", vscdb, exc)
            return

        for key, value in rows:
            composer_id = key.split(":", 1)[1] if ":" in key else key
            meta: dict[str, Any] = {}
            try:
                raw = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

            # Recover cwd from workspaceUri in composerData when workspace.json absent
            resolved_cwd = cwd
            if resolved_cwd is None:
                ws_uri = meta.get("workspaceUri") or meta.get("workspacePath") or ""
                if isinstance(ws_uri, str) and ws_uri.startswith("file://"):
                    resolved_cwd = urllib.parse.unquote(ws_uri[len("file://") :]) or None

            started_at: float | None = None
            raw_ts = meta.get("createdAt") or meta.get("startedAt")
            if isinstance(raw_ts, (int, float)):
                started_at = (
                    float(raw_ts) / 1000.0 if raw_ts > _MIN_VALID_UNIX_MS else float(raw_ts)
                )
            elif isinstance(raw_ts, str):
                with contextlib.suppress(ValueError, TypeError):
                    started_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()

            extra: dict[str, Any] = {
                "storage": "vscdb",
                "composer_id": composer_id,
                "vscdb_path": str(vscdb),
            }
            if resolved_cwd is None:
                extra["unresolved_cwd"] = True

            yield SessionFile(
                source=self.name,
                path=vscdb,
                cwd=resolved_cwd,
                session_id=composer_id,
                started_at=started_at,
                last_modified=mtime,
                extra=extra,
            )

    def _yield_item_table_sessions(
        self,
        conn: sqlite3.Connection,
        vscdb: Path,
        cwd: str | None,
    ) -> Iterator[SessionFile]:
        """Yield one SessionFile for a legacy ItemTable workspace DB."""
        try:
            mtime = vscdb.stat().st_mtime
        except OSError:
            mtime = 0.0

        # Verify at least one known key exists.
        found = False
        for key in _ITEM_TABLE_CHAT_KEYS:
            try:
                row = conn.execute(
                    "SELECT 1 FROM ItemTable WHERE [key] = ? LIMIT 1", (key,)
                ).fetchone()
                if row:
                    found = True
                    break
            except sqlite3.Error:
                continue

        if not found:
            return

        extra: dict[str, Any] = {
            "storage": "vscdb_legacy",
            "vscdb_path": str(vscdb),
        }
        if cwd is None:
            extra["unresolved_cwd"] = True

        session_id = f"legacy_{vscdb.parent.name}"
        yield SessionFile(
            source=self.name,
            path=vscdb,
            cwd=cwd,
            session_id=session_id,
            started_at=None,
            last_modified=mtime,
            extra=extra,
        )

    # ------------------------------------------------------------------
    # iter_records
    # ------------------------------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Dispatch to JSONL or vscdb reader based on session storage type."""
        storage = session.extra.get("storage", "")
        if storage == "vscdb":
            yield from self._iter_vscdb_records(session, start_offset)
        elif storage == "vscdb_legacy":
            yield from self._iter_vscdb_legacy_records(session, start_offset)
        else:
            yield from self._iter_jsonl_records(session, start_offset)

    def _iter_jsonl_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Stream ``(next_byte_offset, Record)`` pairs from a JSONL session.

        Tolerant parser:

        * Blank lines are skipped.
        * Lines that fail ``json.loads`` are skipped (Cursor sometimes
          writes mid-flight when the IDE crashes).
        * Non-dict top-level JSON values are skipped.
        * Translation failures are skipped — Cursor schema drifts between
          versions and we'd rather lose a few rows than abort a tail-read.

        Byte offset semantics match :func:`lib.jsonl_walker.iter_records`:
        the yielded offset is the position *after* the consumed line
        (including its newline). Pass it back as ``start_offset`` to
        resume from there.
        """
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
                    # Truncated tail — drop; the IDE will rewrite.
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    rec = _cursor_line_to_record(obj, session, byte_offset=offset - line_len)
                except Exception:
                    # Tolerant — schema drift should never abort a tail-read.
                    continue
                yield offset, rec

    def _iter_vscdb_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Query ``bubbleId:<composer_id>:*`` keys and yield Records.

        Records are ordered by SQLite ``rowid`` (insertion order), which
        matches conversation sequence. ``start_offset`` is treated as a
        minimum rowid (0 = all rows) for incremental tailing.

        Schema tolerance: every field access is guarded; a corrupt bubble
        is skipped (logged at DEBUG) rather than raising.
        """
        vscdb = Path(session.extra.get("vscdb_path", str(session.path)))
        composer_id = session.extra.get("composer_id", session.session_id)

        try:
            conn = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            log.warning("cursor vscdb iter %s: cannot open: %s", vscdb, exc)
            return

        try:
            rows = conn.execute(
                "SELECT rowid, key, value FROM cursorDiskKV "
                "WHERE key LIKE ? AND rowid > ? "
                "ORDER BY rowid ASC",
                (f"bubbleId:{composer_id}:%", start_offset),
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("cursor vscdb iter %s: query failed: %s", vscdb, exc)
            conn.close()
            return

        conn.close()

        for rowid, key, value in rows:
            try:
                raw = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
                bubble_data = json.loads(raw)
            except (json.JSONDecodeError, TypeError, AttributeError):
                log.debug("cursor vscdb %s: skipping non-JSON bubble row %s", vscdb, key)
                continue
            if not isinstance(bubble_data, dict):
                continue
            try:
                rec = _bubble_to_record(bubble_data, composer_id, session, key)
            except Exception as exc:
                log.debug("cursor vscdb %s: bubble_to_record failed for %s: %s", vscdb, key, exc)
                continue
            yield rowid, rec

    def _iter_vscdb_legacy_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Yield records from a legacy ItemTable workspace vscdb.

        ``start_offset`` is a sequence counter (not byte offset).
        """
        vscdb = Path(session.extra.get("vscdb_path", str(session.path)))

        try:
            conn = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            log.warning("cursor vscdb legacy iter %s: cannot open: %s", vscdb, exc)
            return

        try:
            for seq, rec in _iter_item_table_records(conn, session):
                if seq <= start_offset:
                    continue
                yield seq, rec
        finally:
            with contextlib.suppress(Exception):
                conn.close()


# Register at import time — :func:`lib.sources.all_sources` walks SOURCES.
SOURCES.append(CursorSource)


__all__ = ["CursorSource"]
