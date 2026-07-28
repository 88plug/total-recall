"""OpenCode (sst/opencode) session-source adapter.

OpenCode stores session transcripts in one of two layouts depending on
version:

* **SQLite (current, post-v1.1.53)** — ``opencode.db`` with Drizzle ORM
  tables ``MessageTable``, ``PartTable``, ``SessionTable``,
  ``ProjectTable``. Each row's ``info`` column is opaque JSON whose shape
  is discriminated by ``type`` (for parts) or by the message ``role``.
* **Legacy JSON (pre-SQLite)** — flat per-session and per-message files
  under ``storage/session/<projectHash>/<sessionID>.json`` and
  ``storage/message/<sessionID>/msg_<messageID>.json``.

Both layouts live under ``$OPENCODE_DATA_DIR`` (comma-separated list
supported) or, by default, ``~/.local/share/opencode``.

The adapter normalizes both into :class:`lib.schema.Record` instances so
the rest of total-recall does not need to care which storage backend is
in use.

This module is *defensive* about its base classes — ``lib.sources.base``
is being built concurrently in another worktree; the import has a fallback
so this file is still independently testable.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Defensive base-class import
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised when XW1 lands
    from lib.sources import base as _base_mod

    SOURCES = _base_mod.SOURCES  # type: ignore[misc]
    SessionFile = _base_mod.SessionFile  # type: ignore[misc,assignment]
    SessionSource = _base_mod.SessionSource  # type: ignore[misc,assignment]
    _HAVE_BASE = True
except Exception:  # noqa: BLE001 - any import failure → use stubs
    _HAVE_BASE = False

    @dataclass
    class SessionFile:  # type: ignore[no-redef]
        """Stand-in for ``lib.sources.base.SessionFile``.

        Mirrors the field set the real base module exposes so callers
        constructing ``SessionFile(...)`` work the same against either.
        """

        source: str
        path: Path
        cwd: str | None
        session_id: str
        started_at: float | None = None
        last_modified: float = 0.0
        extra: dict = field(default_factory=dict)

    class SessionSource:  # type: ignore[no-redef]
        """Stand-in protocol for ``lib.sources.base.SessionSource``."""

        name: str = ""

        def is_available(self) -> bool:  # pragma: no cover - stub
            return False

        def discover_sessions(self) -> Iterator[SessionFile]:  # pragma: no cover
            return iter(())

        def iter_records(
            self, session: SessionFile, start_offset: int = 0
        ) -> Iterator[tuple[int, Any]]:  # pragma: no cover
            return iter(())

    SOURCES: list[type[SessionSource]] = []  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Schema imports — Record etc. live in lib.schema and exist today
# ---------------------------------------------------------------------------

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    ToolResult,
    ToolUseRef,
    UserRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``, or an empty set when it is absent."""
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """``True`` iff ``table`` exists in this database."""
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _ts_from_ms(value: Any) -> datetime | None:
    """OpenCode stores epoch milliseconds — convert to UTC datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _remap_tokens(tokens: Any) -> dict[str, Any] | None:
    """Translate OpenCode's nested ``tokens`` block to total-recall's flat
    usage dict.

    OpenCode shape:
        ``{input, output, reasoning, cache: {read, write}}``

    Claude-recall convention (taken from real Anthropic usage payloads):
        ``{input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens, reasoning_tokens}``
    """

    if not isinstance(tokens, dict):
        return None
    out: dict[str, Any] = {}
    if "input" in tokens:
        out["input_tokens"] = tokens.get("input")
    if "output" in tokens:
        out["output_tokens"] = tokens.get("output")
    if "reasoning" in tokens:
        out["reasoning_tokens"] = tokens.get("reasoning")
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else None
    if cache:
        if "read" in cache:
            out["cache_read_tokens"] = cache.get("read")
        if "write" in cache:
            out["cache_creation_tokens"] = cache.get("write")
    return out or None


def _part_to_block(part_info: dict[str, Any]) -> Block | None:
    """Translate one OpenCode part-info dict to an assistant content
    :class:`Block`.

    Returns ``None`` for parts that don't map to assistant content
    (``step-start``, ``step-finish``, ``compaction``, ``subtask`` — those
    are control-flow markers, not turn content).
    """

    if not isinstance(part_info, dict):
        return None
    t = part_info.get("type")
    if t == "text":
        return Block(type="text", text=part_info.get("text", "") or "", raw=part_info)
    if t == "reasoning":
        # OpenCode's reasoning maps onto Anthropic's thinking blocks.
        return Block(
            type="thinking",
            thinking=part_info.get("text", "") or part_info.get("reasoning", "") or "",
            raw=part_info,
        )
    if t == "tool":
        # Tool parts go through a state machine; emit a tool_use block
        # regardless of state. Result/state stays in raw for callers.
        name = part_info.get("name") or part_info.get("tool") or ""
        args = part_info.get("args") or part_info.get("input") or {}
        tu_id = part_info.get("id") or part_info.get("callID") or ""
        return Block(
            type="tool_use",
            tool_use=ToolUseRef(
                id=str(tu_id),
                name=str(name),
                input=args if isinstance(args, dict) else {"value": args},
            ),
            raw=part_info,
        )
    if t == "file":
        # File parts surface as text-shaped blocks with the file URL/name
        # in raw — keep them so iter_records consumers can see attachments.
        return Block(type="text", text=part_info.get("text", "") or "", raw=part_info)
    return None


def _parts_to_tool_results(parts: list[dict[str, Any]]) -> list[ToolResult]:
    """For ``role:"user"`` messages whose parts include completed tool
    invocations (rare in OpenCode — tools usually live on assistant rows —
    but supported for completeness)."""

    out: list[ToolResult] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") != "tool":
            continue
        state = p.get("state") or {}
        if isinstance(state, dict) and state.get("status") in {"success", "error"}:
            text = state.get("result") or state.get("output") or ""
            if not isinstance(text, str):
                text = json.dumps(text)
            out.append(
                ToolResult(
                    tool_use_id=str(p.get("id") or p.get("callID") or ""),
                    is_error=state.get("status") == "error",
                    content=text,
                    raw_content=state,
                )
            )
    return out


def _opencode_to_record(
    message_info: dict[str, Any],
    parts: list[dict[str, Any]],
    *,
    message_id: str,
    session_id: str,
    cwd: str | None,
    role: str,
) -> Record:
    """Translate one OpenCode message+parts pair into the appropriate
    :class:`Record` subclass.

    ``message_info`` is the parsed ``MessageTable.info`` JSON. ``parts`` is
    the list of parsed ``PartTable.info`` JSON dicts for the same message.
    """

    if not isinstance(message_info, dict):
        message_info = {}

    created: datetime | None = None
    time_field = message_info.get("time")
    if isinstance(time_field, dict):
        created = _ts_from_ms(time_field.get("created"))
    elif time_field is not None:
        created = _ts_from_ms(time_field)

    base: dict[str, Any] = dict(
        type=role,
        uuid=message_id,
        parent_uuid=None,
        session_id=session_id,
        ts=created,
        cwd=cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw={"message": message_info, "parts": parts},
        byte_offset=0,
    )

    if role == "assistant":
        _model = message_info.get("model")
        model_block = _model if isinstance(_model, dict) else {}
        model_id: str | None = None
        if isinstance(model_block, dict):
            model_id = model_block.get("modelID") or model_block.get("model")
        if not model_id and isinstance(message_info.get("model"), str):
            model_id = message_info.get("model")

        blocks: list[Block] = []
        for p in parts:
            blk = _part_to_block(p)
            if blk is not None:
                blocks.append(blk)

        usage = _remap_tokens(message_info.get("tokens"))
        finish = message_info.get("finish") if isinstance(message_info.get("finish"), dict) else {}
        stop_reason = finish.get("reason") if isinstance(finish, dict) else None

        return AssistantRecord(
            **base,
            model=model_id,
            content=blocks,
            usage=usage,
            stop_reason=stop_reason,
            message_id=message_id,
            request_id=None,
        )

    # Default to user role for anything that isn't assistant; OpenCode
    # only emits "user" and "assistant" today.
    text_parts: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            t = p.get("text")
            if isinstance(t, str) and t:
                text_parts.append(t)
    text = "\n".join(text_parts) if text_parts else None
    tool_results = _parts_to_tool_results(parts)
    kind = "tool_result" if tool_results else ("string" if text else "empty")

    return UserRecord(
        **base,
        content_kind=kind,
        text=text,
        tool_results=tool_results,
        tool_use_result_payload=None,
        is_compact_summary=False,
        is_meta=False,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenCodeSource(SessionSource):
    """Session source that reads OpenCode's local data directory."""

    name = "opencode"

    def __init__(self, data_dirs: list[Path] | None = None) -> None:
        self.data_dirs: list[Path] = (
            list(data_dirs) if data_dirs is not None else self._resolve_data_dirs()
        )
        self.db_paths: list[Path] = [
            d / "opencode.db" for d in self.data_dirs if (d / "opencode.db").exists()
        ]
        self.legacy_dirs: list[Path] = [
            d / "storage" for d in self.data_dirs if (d / "storage").exists()
        ]

    # -- discovery roots ----------------------------------------------------

    @staticmethod
    def _resolve_data_dirs() -> list[Path]:
        env = os.environ.get("OPENCODE_DATA_DIR")
        if env:
            paths = [Path(p).expanduser() for p in env.split(",") if p.strip()]
            if paths:
                return paths
        return [Path.home() / ".local" / "share" / "opencode"]

    def is_available(self) -> bool:
        return bool(self.db_paths) or bool(self.legacy_dirs)

    # -- session discovery --------------------------------------------------

    def discover_sessions(self) -> Iterator[SessionFile]:
        for db_path in self.db_paths:
            yield from self._discover_sqlite(db_path)
        for legacy in self.legacy_dirs:
            yield from self._discover_legacy_json(legacy)

    def _discover_sqlite(self, db_path: Path) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per session present in
        ``db_path``.

        We *try* to read a sessions/projects table to get the cwd, but
        OpenCode has renamed these a few times. Fall back gracefully:
        worst case the cwd is ``None`` and the caller derives it later.
        """

        if not db_path.exists():
            return
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            try:
                stat = db_path.stat()
                last_modified = stat.st_mtime
            except OSError:
                last_modified = 0.0

            session_cwds = self._session_cwds_sqlite(conn)

            # Group messages by session_id. OpenCode 2026 uses lowercase
            # snake_case Drizzle table names (`message`, `session`, `part`,
            # `project`); older builds used the camelCase `*Table` form.
            # Try the modern shape first; fall back to legacy.
            try:
                cur = conn.execute("SELECT session_id, MIN(id) FROM message GROUP BY session_id")
            except sqlite3.Error:
                try:
                    cur = conn.execute(
                        "SELECT sessionID, MIN(id) FROM MessageTable GROUP BY sessionID"
                    )
                except sqlite3.Error:
                    return
            for row in cur.fetchall():
                session_id = row[0]
                if not session_id:
                    continue
                cwd = session_cwds.get(str(session_id))
                yield SessionFile(
                    source=self.name,
                    path=db_path,
                    cwd=cwd,
                    session_id=str(session_id),
                    started_at=None,
                    last_modified=last_modified,
                    extra={"storage": "sqlite", "db_path": str(db_path)},
                )
        finally:
            conn.close()

    @staticmethod
    def _session_cwds_sqlite(conn: sqlite3.Connection) -> dict[str, str | None]:  # noqa: C901
        """Best-effort join of session → project → cwd. OpenCode schema
        has shifted; try a couple of column names before giving up."""

        out: dict[str, str | None] = {}

        # 0) OpenCode 2026: `session` table has top-level `directory` column.
        #    Child (sub-agent) sessions store `directory = ''` and keep the real
        #    path on their project row, so fall back through
        #    `session.path` → `project.worktree` before giving up.
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(session)")}
        except sqlite3.Error:
            cols = set()

        if "id" in cols:
            select = ["s.id", "s.directory" if "directory" in cols else "NULL"]
            select.append("s.path" if "path" in cols else "NULL")
            join = ""
            if "project_id" in cols and _has_table(conn, "project"):
                pcols = _table_columns(conn, "project")
                if "worktree" in pcols:
                    select.append("p.worktree")
                    join = " LEFT JOIN project p ON p.id = s.project_id"
                else:
                    select.append("NULL")
            else:
                select.append("NULL")
            try:
                cur = conn.execute(f"SELECT {', '.join(select)} FROM session s{join}")
                for sid, directory, path, worktree in cur.fetchall():
                    if not sid:
                        continue
                    # First non-empty wins; '' is as absent as NULL here.
                    out[str(sid)] = directory or path or worktree or None
                if out:
                    return out
            except sqlite3.Error:
                out.clear()

        # 1) Legacy `SessionTable` with an `info` JSON column carrying a
        #    `directory` / `path` / `cwd` key.
        try:
            cur = conn.execute("SELECT id, info FROM SessionTable")
            for sid, info in cur.fetchall():
                if not sid:
                    continue
                cwd: str | None = None
                if isinstance(info, str):
                    try:
                        info_obj = json.loads(info)
                    except json.JSONDecodeError:
                        info_obj = None
                    if isinstance(info_obj, dict):
                        cwd = (
                            info_obj.get("directory") or info_obj.get("path") or info_obj.get("cwd")
                        )
                        proj = info_obj.get("project")
                        if not cwd and isinstance(proj, dict):
                            cwd = proj.get("directory") or proj.get("path") or proj.get("worktree")
                out[str(sid)] = cwd
            if out:
                return out
        except sqlite3.Error:
            pass

        # 2) Fall back to a flat ProjectTable join, if present.
        try:
            cur = conn.execute(
                "SELECT s.id, p.path FROM SessionTable s "
                "LEFT JOIN ProjectTable p ON s.projectID = p.id"
            )
            for sid, path in cur.fetchall():
                if sid:
                    out[str(sid)] = path
        except sqlite3.Error:
            pass

        return out

    def _discover_legacy_json(self, storage_root: Path) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per legacy
        ``storage/session/<projectHash>/<sessionID>.json`` file."""

        session_root = storage_root / "session"
        if not session_root.is_dir():
            return
        for project_dir in sorted(session_root.iterdir()):
            if not project_dir.is_dir():
                continue
            for session_file in sorted(project_dir.iterdir()):
                if not session_file.is_file() or session_file.suffix != ".json":
                    continue
                session_id = session_file.stem
                cwd: str | None = None
                try:
                    with session_file.open() as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        cwd = data.get("directory") or data.get("path") or data.get("cwd")
                        proj = data.get("project")
                        if not cwd and isinstance(proj, dict):
                            cwd = proj.get("directory") or proj.get("path") or proj.get("worktree")
                except (OSError, json.JSONDecodeError):
                    pass
                try:
                    stat = session_file.stat()
                    last_modified = stat.st_mtime
                except OSError:
                    last_modified = 0.0
                yield SessionFile(
                    source=self.name,
                    path=session_file,
                    cwd=cwd,
                    session_id=session_id,
                    started_at=None,
                    last_modified=last_modified,
                    extra={
                        "storage": "legacy",
                        "storage_root": str(storage_root),
                        "project_hash": project_dir.name,
                    },
                )

    # -- record iteration ---------------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Record]]:
        storage = (session.extra or {}).get("storage")
        if storage == "sqlite":
            yield from self._iter_sqlite_records(session, start_offset)
        else:
            yield from self._iter_legacy_records(session, start_offset)

    def _iter_sqlite_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Record]]:
        db_path = Path(session.extra.get("db_path") or session.path)
        if not db_path.exists():
            return
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            # OpenCode 2026: lowercase `message` / `part` with `data` JSON
            # column + `session_id` FK. Fall back to legacy `MessageTable`
            # / `PartTable` / `info` for older installs.
            msg_rows: list[tuple] = []
            try:
                msg_cur = conn.execute(
                    "SELECT id, NULL, data FROM message "
                    "WHERE session_id = ? ORDER BY time_created, id",
                    (session.session_id,),
                )
                msg_rows = msg_cur.fetchall()
            except sqlite3.Error:
                try:
                    msg_cur = conn.execute(
                        "SELECT id, role, info FROM MessageTable WHERE sessionID = ? ORDER BY id",
                        (session.session_id,),
                    )
                    msg_rows = msg_cur.fetchall()
                except sqlite3.Error:
                    return

            part_rows: list[tuple] = []
            try:
                part_cur = conn.execute(
                    "SELECT message_id, data FROM part "
                    "WHERE session_id = ? ORDER BY time_created, id",
                    (session.session_id,),
                )
                part_rows = part_cur.fetchall()
            except sqlite3.Error:
                try:
                    part_cur = conn.execute(
                        "SELECT messageID, info FROM PartTable WHERE sessionID = ? ORDER BY id",
                        (session.session_id,),
                    )
                    part_rows = part_cur.fetchall()
                except sqlite3.Error:
                    part_rows = []

            parts_by_msg: dict[str, list[dict[str, Any]]] = {}
            for mid, info_blob in part_rows:
                if not mid:
                    continue
                try:
                    obj = json.loads(info_blob) if isinstance(info_blob, str) else info_blob
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    parts_by_msg.setdefault(str(mid), []).append(obj)

            cwd = session.cwd
            for idx, (mid, role, info_blob) in enumerate(msg_rows):
                if idx < start_offset:
                    continue
                try:
                    info_obj = (
                        json.loads(info_blob) if isinstance(info_blob, str) else (info_blob or {})
                    )
                except json.JSONDecodeError:
                    info_obj = {}
                # 2026 schema embeds role inside data JSON; legacy had it
                # as a top-level column. Prefer the data-embedded one.
                resolved_role = (
                    (info_obj.get("role") if isinstance(info_obj, dict) else None) or role or "user"
                )
                parts = parts_by_msg.get(str(mid), [])
                rec = _opencode_to_record(
                    info_obj,
                    parts,
                    message_id=str(mid),
                    session_id=session.session_id,
                    cwd=cwd,
                    role=str(resolved_role),
                )
                yield idx + 1, rec
        finally:
            conn.close()

    def _iter_legacy_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Record]]:
        storage_root = Path(session.extra.get("storage_root") or "")
        if not storage_root.is_dir():
            return
        msg_dir = storage_root / "message" / session.session_id
        if not msg_dir.is_dir():
            return
        # Stable ordering by filename — OpenCode names them ``msg_<ulid>``
        # which sorts chronologically.
        msg_files = sorted(p for p in msg_dir.iterdir() if p.suffix == ".json")
        for idx, mfile in enumerate(msg_files):
            if idx < start_offset:
                continue
            try:
                with mfile.open() as f:
                    obj = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            # Legacy layout stores the message envelope as
            # ``{id, role, info, parts}``. Tolerate either flat or nested.
            mid = obj.get("id") or mfile.stem
            role = obj.get("role") or "user"
            info_raw = obj.get("info")
            info_obj: dict[str, Any] = info_raw if isinstance(info_raw, dict) else obj
            parts_raw = obj.get("parts")
            parts: list[dict[str, Any]] = parts_raw if isinstance(parts_raw, list) else []
            rec = _opencode_to_record(
                info_obj,
                parts,
                message_id=str(mid),
                session_id=session.session_id,
                cwd=session.cwd,
                role=str(role),
            )
            yield idx + 1, rec


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
#
# Append the *class* (not an instance) to SOURCES, matching the pattern
# established by ``lib.sources.claude_code``. When the stub is in use
# SOURCES is a local list; either way registration is idempotent against
# re-imports.

if _HAVE_BASE:
    try:
        if OpenCodeSource not in SOURCES:
            SOURCES.append(OpenCodeSource)
    except Exception:  # noqa: BLE001 - defensive: never break import
        pass


__all__ = ["OpenCodeSource"]
