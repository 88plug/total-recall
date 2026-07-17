"""Goose (block/goose) session-source adapter — SQLite.

Goose stores session transcripts in a single SQLite DB at
``~/.local/share/goose/sessions/sessions.db``. Two tables matter:

``sessions``
  One row per session. Key columns:
  - ``id`` (TEXT PK, e.g. ``"20260607_3"``) — session id.
  - ``name`` / ``description`` — human/auto labels.
  - ``working_dir`` (TEXT NOT NULL) — the cwd. Ground truth for project.
  - ``created_at`` / ``updated_at`` (TIMESTAMP, ``"YYYY-MM-DD HH:MM:SS"``).
  - ``provider_name`` (TEXT) — e.g. ``"xai_oauth"``.
  - ``model_config_json`` (TEXT) — JSON with ``model_name``, ``context_limit``.
  - ``total_tokens`` / ``input_tokens`` / ``output_tokens`` (INTEGER).
  - ``session_type``, ``goose_mode``, ``archived_at``, ``project_id``.

``messages``
  One row per turn. Linked to a session via ``session_id`` (FK →
  ``sessions.id``). Columns:
  - ``id`` (INTEGER PK AUTOINCREMENT) — insertion order == conversation order.
  - ``message_id`` (TEXT) — Goose message id.
  - ``session_id`` (TEXT NOT NULL) — FK to ``sessions.id``.
  - ``role`` (TEXT NOT NULL) — only ``"user"`` / ``"assistant"`` observed.
  - ``content_json`` (TEXT NOT NULL) — JSON array of content blocks.
  - ``created_timestamp`` (INTEGER) — Unix **seconds**.
  - ``timestamp`` (TIMESTAMP) — ``"YYYY-MM-DD HH:MM:SS"`` mirror.
  - ``tokens`` (INTEGER, often NULL).
  - ``metadata_json`` (TEXT) — e.g. ``{"userVisible":true,"agentVisible":true}``.

Content block shapes (``content_json`` is always a JSON array):
  - ``{"type":"text","text":...}`` — plain text (both roles).
  - ``{"type":"thinking","thinking":...,"signature":...}`` — reasoning.
  - ``{"type":"toolRequest","id":...,"toolCall":{"status","value":{"name",
    "arguments"}},"_meta":{"goose_extension"}}`` — assistant tool call.
  - ``{"type":"toolResponse","id":...,"toolResult":{"status","value":{
    "content":[{"type":"text","text"}],"structuredContent":{...}}}}`` —
    tool output (appears under ``role:"user"``, mirroring Anthropic's
    tool-result-as-user-turn convention).

Mapping to :mod:`lib.schema`:
  - ``role:"assistant"`` → :class:`AssistantRecord`. ``text``/``thinking``
    blocks map directly; ``toolRequest`` → ``Block(type="tool_use",
    tool_use=ToolUseRef(id, name, input=arguments))``.
  - ``role:"user"`` → :class:`UserRecord`. If the content carries
    ``toolResponse`` blocks → ``content_kind="tool_result"`` with one
    :class:`ToolResult` per block; otherwise → ``content_kind="string"``
    (or ``text_array``) with flattened ``text``.

Cursor semantics: :meth:`iter_records` orders by ``messages.id`` (the
autoincrement PK == insertion order == conversation order) and treats
``start_offset`` as a minimum row id for incremental tailing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    ToolResult,
    ToolUseRef,
    UserRecord,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_unix_seconds(raw: Any) -> datetime | None:
    """Parse Goose ``created_timestamp`` (Unix seconds) to aware datetime."""
    if not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _parse_sql_timestamp(raw: Any) -> float | None:
    """Parse a ``"YYYY-MM-DD HH:MM:SS"`` SQL timestamp to Unix seconds.

    Goose writes these in local/naive form; we treat them as UTC for a
    stable, monotonic ``started_at`` (only used for ordering/labels).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    with contextlib.suppress(ValueError, TypeError):
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp()
    return None


def _decode(value: Any) -> Any:
    """Decode a TEXT/BLOB column value to a Python object via ``json.loads``.

    Returns ``None`` on anything that isn't decodable JSON.
    """
    if value is None:
        return None
    try:
        raw = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


def _flatten_tool_result_content(value: Any) -> tuple[str, Any]:
    """Flatten a Goose ``toolResult.value`` to text; keep raw in second slot.

    ``value`` shape: ``{"content":[{"type":"text","text":...}, ...],
    "structuredContent":{...}}``. We join the text blocks; if none, fall
    back to ``structuredContent.stdout`` then the JSON-dumped value.
    """
    if isinstance(value, dict):
        parts: list[str] = []
        for b in value.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
        if parts:
            return "\n".join(parts), value
        sc = value.get("structuredContent")
        if isinstance(sc, dict):
            out = sc.get("stdout")
            if isinstance(out, str) and out:
                return out, value
    if isinstance(value, str):
        return value, value
    return "", value


def _assistant_blocks(content: Any) -> list[Block]:
    """Normalize a Goose assistant ``content_json`` array into Blocks."""
    if not isinstance(content, list):
        return []
    out: list[Block] = []
    for b in content:
        if not isinstance(b, dict):
            out.append(Block(type="?", raw={"value": b}))
            continue
        bt = b.get("type", "?")
        if bt == "text":
            out.append(Block(type="text", text=b.get("text", ""), raw=b))
        elif bt == "thinking":
            out.append(
                Block(
                    type="thinking",
                    thinking=b.get("thinking", ""),
                    thinking_signature=b.get("signature"),
                    raw=b,
                )
            )
        elif bt == "toolRequest":
            call = b.get("toolCall") or {}
            val = call.get("value") if isinstance(call, dict) else {}
            val = val if isinstance(val, dict) else {}
            args = val.get("arguments")
            out.append(
                Block(
                    type="tool_use",
                    tool_use=ToolUseRef(
                        id=str(b.get("id", "")),
                        name=str(val.get("name", "")),
                        input=args if isinstance(args, dict) else {},
                    ),
                    raw=b,
                )
            )
        else:
            # toolResponse should not appear on assistant turns, but keep
            # any unknown block type around rather than dropping it.
            out.append(Block(type=bt, raw=b))
    return out


def _user_tool_results(content: Any) -> list[ToolResult]:
    """Extract :class:`ToolResult` from a user-turn ``toolResponse`` array."""
    out: list[ToolResult] = []
    if not isinstance(content, list):
        return out
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "toolResponse"):
            continue
        result = b.get("toolResult") or {}
        status = result.get("status") if isinstance(result, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        text, raw_c = _flatten_tool_result_content(value)
        out.append(
            ToolResult(
                tool_use_id=str(b.get("id", "")),
                is_error=(status not in (None, "success")),
                content=text,
                raw_content=raw_c,
            )
        )
    return out


def _user_text(content: Any) -> str | None:
    """Flatten user ``text`` blocks to a single string."""
    if not isinstance(content, list):
        return None
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    joined = "\n".join(p for p in parts if p)
    return joined or None


def _row_to_record(row: dict[str, Any], session: SessionFile) -> Record:
    """Translate one ``messages`` row dict into a canonical Record."""
    role = row.get("role")
    message_id = row.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        message_id = str(message_id)

    content = _decode(row.get("content_json"))
    ts = _parse_unix_seconds(row.get("created_timestamp"))

    base = dict(
        type=role if isinstance(role, str) else "?",
        uuid=message_id,
        parent_uuid=None,
        session_id=session.session_id,
        ts=ts,
        cwd=session.cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw={"row": {k: v for k, v in row.items() if k != "content_json"}, "content": content},
        byte_offset=0,
    )

    if role == "assistant":
        return AssistantRecord(
            **base,
            model=session.extra.get("model"),
            content=_assistant_blocks(content),
            usage=None,
            stop_reason=None,
            message_id=message_id,
            request_id=None,
        )

    if role == "user":
        tool_results = _user_tool_results(content)
        if tool_results:
            return UserRecord(
                **base,
                content_kind="tool_result",
                text=None,
                tool_results=tool_results,
                tool_use_result_payload=None,
                is_compact_summary=False,
                is_meta=False,
            )
        text = _user_text(content)
        return UserRecord(
            **base,
            content_kind="string" if text else "empty",
            text=text,
            tool_results=[],
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=False,
        )

    return Record(**base)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GooseSource(SessionSource):
    """Goose (block/goose) adapter — single SQLite DB.

    The DB path is overridable for tests; production uses the platform
    default ``~/.local/share/goose/sessions/sessions.db``.
    """

    name = "goose"

    _DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "goose"

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        db_path: Path | None = None,
        include_archived: bool = False,
    ) -> None:
        import os

        self.include_archived = include_archived
        if db_path is not None:
            # Legacy/direct override — used internally when data_dir is known.
            self.db_path = db_path
        else:
            root = (
                data_dir
                if data_dir is not None
                else Path(os.environ["GOOSE_DATA_DIR"])
                if "GOOSE_DATA_DIR" in os.environ
                else self._DEFAULT_DATA_DIR
            )
            self.db_path = root / "sessions" / "sessions.db"

    # ------------------------------------------------------------------
    # is_available
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """``True`` iff the Goose sessions DB exists on disk."""
        return self.db_path.is_file()

    # ------------------------------------------------------------------
    # discover_sessions
    # ------------------------------------------------------------------

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per row in ``sessions``.

        Lazy w.r.t. message bodies — only the ``sessions`` table is read.
        Ordered by ``id`` for stable checkpointing.
        """
        if not self.db_path.is_file():
            return

        try:
            mtime = self.db_path.stat().st_mtime
        except OSError:
            mtime = 0.0

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            log.warning("goose db %s: cannot open: %s", self.db_path, exc)
            return

        try:
            conn.row_factory = sqlite3.Row
            archive_clause = "" if self.include_archived else "WHERE s.archived_at IS NULL"
            rows = conn.execute(
                "SELECT s.id, s.name, s.description, s.working_dir, s.created_at, s.updated_at, "
                "s.provider_name, s.model_config_json, s.session_type, s.goose_mode, "
                "s.total_tokens, s.input_tokens, s.output_tokens, s.archived_at, s.project_id, "
                "COUNT(m.id) AS msg_count "
                "FROM sessions s "
                "LEFT JOIN messages m ON m.session_id = s.id "
                f"{archive_clause} "
                "GROUP BY s.id ORDER BY s.id ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("goose db %s: sessions query failed: %s", self.db_path, exc)
            return
        finally:
            with contextlib.suppress(Exception):
                conn.close()

        for row in rows:
            if (row["msg_count"] or 0) == 0:
                continue
            session_id = row["id"]
            if session_id is None:
                continue
            session_id = str(session_id)

            cwd = row["working_dir"] or None
            started_at = _parse_sql_timestamp(row["created_at"])

            model: str | None = None
            mcfg = _decode(row["model_config_json"])
            if isinstance(mcfg, dict):
                mn = mcfg.get("model_name")
                if isinstance(mn, str) and mn:
                    model = mn

            extra: dict[str, Any] = {
                "storage": "sqlite",
                "db_path": str(self.db_path),
                "name": row["name"] or "",
                "description": row["description"] or "",
                "session_type": row["session_type"],
                "goose_mode": row["goose_mode"],
                "provider": row["provider_name"],
                "model": model,
                "total_tokens": row["total_tokens"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "archived_at": row["archived_at"],
                "project_id": row["project_id"],
            }
            if cwd is None:
                extra["unresolved_cwd"] = True

            yield SessionFile(
                source=self.name,
                path=self.db_path,
                cwd=cwd,
                session_id=session_id,
                started_at=started_at,
                last_modified=mtime,
                extra=extra,
            )

    # ------------------------------------------------------------------
    # iter_records
    # ------------------------------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Yield ``(row_id, Record)`` for ``session``, ordered by ``messages.id``.

        ``start_offset`` is a minimum row id (0 = all rows) for incremental
        tailing — the autoincrement PK is insertion == conversation order.
        Corrupt rows are skipped (logged at DEBUG) rather than raising.
        """
        db_path = Path(session.extra.get("db_path", str(session.path)))

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            log.warning("goose db iter %s: cannot open: %s", db_path, exc)
            return

        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, message_id, session_id, role, content_json, "
                "created_timestamp, tokens, metadata_json FROM messages "
                "WHERE session_id = ? AND id > ? ORDER BY id ASC",
                (session.session_id, start_offset),
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("goose db iter %s: query failed: %s", db_path, exc)
            return
        finally:
            with contextlib.suppress(Exception):
                conn.close()

        for row in rows:
            row_id = row["id"]
            # Skip rows with unparseable content_json — corrupt rows should not
            # surface as empty records (they'd pollute the index with noise).
            if _decode(row["content_json"]) is None and row["content_json"] is not None:
                log.debug("goose db %s: skipping corrupt content_json at id=%s", db_path, row_id)
                continue
            try:
                rec = _row_to_record(dict(row), session)
            except Exception as exc:  # noqa: BLE001 — tolerant tail-read
                log.debug("goose db %s: row_to_record failed for id=%s: %s", db_path, row_id, exc)
                continue
            yield row_id, rec


# Register at import time — :func:`lib.sources.all_sources` walks SOURCES.
SOURCES.append(GooseSource)


# ---------------------------------------------------------------------------
# Test-facing helpers (thin aliases over the internal functions)
# ---------------------------------------------------------------------------


def _ts_from_epoch_s(raw: Any) -> datetime | None:
    """Alias for tests: convert epoch-seconds int to aware datetime."""
    return _parse_unix_seconds(raw)


def _goose_msg_to_record(
    *,
    role: str,
    content: Any,
    ts: Any,
    message_id: str | None,
    session: SessionFile,
) -> Record:
    """Alias for tests: build a Record from explicit message fields."""
    row = {
        "role": role,
        "content_json": content if isinstance(content, str) else __import__("json").dumps(content),
        "created_timestamp": ts,
        "message_id": message_id,
    }
    return _row_to_record(row, session)


__all__ = ["GooseSource", "_ts_from_epoch_s", "_goose_msg_to_record"]
