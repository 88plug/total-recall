"""Cline (saoudrizwan/cline) adapter.

Cline is a VS Code extension and (newer) CLI that runs autonomous coding
tasks. Each task gets its own directory of JSON files:

* ``api_conversation_history.json`` — canonical LLM stream as an array of
  ``Anthropic.MessageParam`` objects (``{role, content}`` where content
  is a string or list of typed blocks: ``text``, ``tool_use``,
  ``tool_result``, ``image``).
* ``ui_messages.json`` — UI ``ask``/``say`` events. Mostly redundant
  with the canonical stream but carries human-readable titles.
* ``task_metadata.json`` — ``{files_in_context, model_usage,
  environment_history}``.
* ``context_history.json`` / ``settings.json`` — auxiliary.

The session index lives at ``<data_dir>/state/taskHistory.json`` —
``HistoryItem[]`` with ``id``, ``ts``, ``task``, ``workspace`` (the
canonical cwd hint — Cline doesn't track cwd per-message), token usage,
and the model id. We read it for titles, cwd, and model.

Storage roots
=============

We probe the following locations in priority order:

1. ``$CLINE_HOME`` if set.
2. ``~/.cline/data`` — the CLI's default.
3. Platform-specific VS Code ``globalStorage`` directories for both the
   modern ``cline.cline`` extension id and the legacy
   ``saoudrizwan.claude-dev`` id (Cursor still uses the legacy id).

Each candidate that contains a ``tasks/`` subdirectory contributes to
the merged session list.

Translation
===========

Each entry in ``api_conversation_history.json`` becomes one canonical
:class:`lib.schema.Record`:

* ``role == "user"`` carrying ``tool_result`` blocks → :class:`UserRecord`
  with ``content_kind="tool_result"`` and the result text in
  :attr:`UserRecord.tool_results`.
* ``role == "user"`` with plain text or text blocks → :class:`UserRecord`
  with the appropriate ``content_kind``.
* ``role == "assistant"`` → :class:`AssistantRecord`. ``tool_use`` blocks
  in ``content`` become ``Block(type="tool_use", tool_use=...)``; text
  and thinking blocks pass through. Model is filled from the task
  history entry (Cline doesn't repeat it per-turn).

``task_metadata.json``'s ``model_usage`` array, when present, is summed
and attached to the first assistant record's :attr:`AssistantRecord.usage`
so downstream cost / token logic sees it.

The ``byte_offset`` slot stores the *index into the
api_conversation_history array* (Cline rewrites the file each turn, so a
real byte offset has no checkpointing value — but the array index is
monotonic and lets the ingest loop resume).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    ToolResult,
    ToolUseRef,
    UserRecord,
    _classify_user_content,
    _extract_user_text,
    _normalize_tool_result_content,
    _parse_ts,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


# Modern vs. legacy VS Code extension ids. We probe both because Cursor
# and older installs still use the legacy id.
_EXT_IDS = ("cline.cline", "saoudrizwan.claude-dev")


def _vscode_global_storage_roots() -> list[Path]:
    """Return the per-platform user-data roots that may host globalStorage.

    We include both VS Code and Cursor because Cline runs in both. Each
    returned path is the ``<userDataDir>/User/globalStorage`` directory;
    callers append the extension id to land at the storage dir.
    """

    home = Path.home()
    roots: list[Path] = []
    if sys.platform == "darwin":
        roots.extend(
            [
                home / "Library" / "Application Support" / "Code" / "User" / "globalStorage",
                home / "Library" / "Application Support" / "Code - Insiders"
                / "User" / "globalStorage",
                home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage",
            ]
        )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata)
            roots.extend(
                [
                    base / "Code" / "User" / "globalStorage",
                    base / "Code - Insiders" / "User" / "globalStorage",
                    base / "Cursor" / "User" / "globalStorage",
                ]
            )
    else:
        # Linux + everything else uses XDG-style ~/.config.
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        roots.extend(
            [
                cfg / "Code" / "User" / "globalStorage",
                cfg / "Code - Insiders" / "User" / "globalStorage",
                cfg / "Cursor" / "User" / "globalStorage",
            ]
        )
    return roots


def _default_data_dirs() -> list[Path]:
    """Discover every directory that might contain Cline ``tasks/``.

    Priority order: ``$CLINE_HOME``, then ``~/.cline/data``, then every
    platform-specific globalStorage candidate for both extension ids.
    Duplicates are removed but order is preserved.
    """

    out: list[Path] = []
    env = os.environ.get("CLINE_HOME")
    if env:
        out.append(Path(env).expanduser())

    out.append(Path.home() / ".cline" / "data")

    for gs_root in _vscode_global_storage_roots():
        for ext_id in _EXT_IDS:
            out.append(gs_root / ext_id)

    # De-dupe while preserving order.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    return deduped


def _safe_load_json(path: Path) -> Any | None:
    """Return parsed JSON or ``None`` on any read / parse error."""

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _split_anthropic_user_content(
    content: Any,
) -> tuple[str, list[ToolResult], list[dict[str, Any]]]:
    """Split a user ``MessageParam`` content into (text, tool_results, other).

    Cline frequently packs ``tool_result`` blocks alongside plain ``text``
    blocks in the same user message (the canonical Anthropic shape).
    Surfacing both keeps the resulting :class:`UserRecord` faithful.
    """

    text_parts: list[str] = []
    tool_results: list[ToolResult] = []
    other: list[dict[str, Any]] = []

    if isinstance(content, str):
        return content, [], []

    if not isinstance(content, list):
        return "", [], []

    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            text_parts.append(b.get("text", "") or "")
        elif bt == "tool_result":
            text, raw_c = _normalize_tool_result_content(b.get("content", ""))
            tool_results.append(
                ToolResult(
                    tool_use_id=b.get("tool_use_id", "") or "",
                    is_error=bool(b.get("is_error", False)),
                    content=text,
                    raw_content=raw_c,
                )
            )
        else:
            other.append(b)

    return "\n".join(p for p in text_parts if p), tool_results, other


def _assistant_blocks_from_anthropic(content: Any) -> list[Block]:
    """Translate ``MessageParam.content`` for an assistant turn to Blocks."""

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


def _sum_model_usage(model_usage: Any) -> dict[str, Any] | None:
    """Sum a ``task_metadata.json`` ``model_usage`` array into a usage dict.

    Returns ``None`` when nothing is summable. Output keys match the
    Anthropic shape Claude Code uses elsewhere
    (``input_tokens`` / ``output_tokens`` / ``cache_*``).
    """

    if not isinstance(model_usage, list) or not model_usage:
        return None
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    any_nonzero = False
    for row in model_usage:
        if not isinstance(row, dict):
            continue
        # Cline uses snake_case here.
        for k in totals:
            v = row.get(k, 0)
            if isinstance(v, (int, float)):
                totals[k] += int(v)
                if v:
                    any_nonzero = True
    if not any_nonzero:
        return None
    return totals


def _build_base_kwargs(
    raw: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None,
    index: int,
    ts_seed: Any | None,
) -> dict[str, Any]:
    """Cross-cutting Record fields for a Cline message."""

    msg_type = raw.get("role", "?") if isinstance(raw, dict) else "?"
    return dict(
        type=msg_type,
        uuid=None,  # Anthropic MessageParam has no per-message id.
        parent_uuid=None,
        session_id=session_id,
        ts=_parse_ts(ts_seed) if isinstance(ts_seed, str) else None,
        cwd=cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw=raw,
        byte_offset=index,
    )


def _project_message_to_record(
    msg: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None,
    index: int,
    model: str | None,
    attach_usage: dict[str, Any] | None,
) -> Record:
    """Translate one Anthropic ``MessageParam`` into a canonical Record."""

    base = _build_base_kwargs(
        msg,
        session_id=session_id,
        cwd=cwd,
        index=index,
        ts_seed=msg.get("ts") if isinstance(msg, dict) else None,
    )
    role = msg.get("role")
    content = msg.get("content")

    if role == "assistant":
        return AssistantRecord(
            **base,
            model=model,
            content=_assistant_blocks_from_anthropic(content),
            usage=attach_usage,
            stop_reason=None,
            message_id=None,
            request_id=None,
        )

    if role == "user":
        # Distinguish "pure tool-result envelope" from "plain user prompt"
        # so downstream code can tell them apart the same way it does for
        # Claude Code.
        text, tool_results, other = _split_anthropic_user_content(content)
        if tool_results and not text and not other:
            kind = "tool_result"
        elif tool_results:
            # Mixed: text + tool_result. Classify as tool_result; the text
            # is still in raw.
            kind = "tool_result"
        else:
            kind = _classify_user_content(content)
            text = _extract_user_text(content) or text or None
        return UserRecord(
            **base,
            content_kind=kind,
            text=text or None,
            tool_results=tool_results,
            tool_use_result_payload=None,
            is_compact_summary=False,
            is_meta=False,
        )

    return Record(**base)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ClineSource(SessionSource):
    """Adapter for Cline task storage (VS Code extension + CLI).

    In production, construct with no arguments — paths are auto-resolved
    in priority order via :func:`_default_data_dirs`. Tests pass an
    explicit ``data_dirs`` list to point at a synthetic tree.
    """

    name = "cline"

    def __init__(self, data_dirs: list[Path] | None = None) -> None:
        if data_dirs is not None:
            self.data_dirs = list(data_dirs)
        else:
            self.data_dirs = self._resolve_data_dirs()

    @staticmethod
    def _resolve_data_dirs() -> list[Path]:
        return _default_data_dirs()

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """``True`` if any candidate data dir has a ``tasks/`` subdir.

        Cheap: only ``Path.is_dir()`` per candidate.
        """

        return any((d / "tasks").is_dir() for d in self.data_dirs)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _load_task_history(self, data_dir: Path) -> dict[str, dict[str, Any]]:
        """Return a ``{taskId: HistoryItem}`` map for one data dir."""

        path = data_dir / "state" / "taskHistory.json"
        body = _safe_load_json(path)
        out: dict[str, dict[str, Any]] = {}
        if isinstance(body, list):
            for entry in body:
                if isinstance(entry, dict):
                    tid = entry.get("id")
                    if isinstance(tid, str):
                        out[tid] = entry
        return out

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per task with a canonical history.

        Tasks whose ``api_conversation_history.json`` is missing or
        unreadable are skipped — without the canonical stream we have
        nothing to ingest. Order is data-dir-then-task-id (stable for
        checkpointing).
        """

        for data_dir in self.data_dirs:
            tasks_dir = data_dir / "tasks"
            if not tasks_dir.is_dir():
                continue
            history = self._load_task_history(data_dir)
            try:
                entries = sorted(tasks_dir.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for task_dir in entries:
                if not task_dir.is_dir():
                    continue
                api_conv = task_dir / "api_conversation_history.json"
                if not api_conv.is_file():
                    continue
                try:
                    stat = api_conv.stat()
                except OSError:
                    continue
                hist = history.get(task_dir.name, {})
                workspace = hist.get("workspace") if isinstance(hist, dict) else None
                if not isinstance(workspace, str):
                    workspace = None
                title = hist.get("task") if isinstance(hist, dict) else None
                model = hist.get("modelId") if isinstance(hist, dict) else None
                started_at: float | None = None
                ts_raw = hist.get("ts") if isinstance(hist, dict) else None
                if isinstance(ts_raw, (int, float)):
                    # Cline records ms epoch.
                    started_at = float(ts_raw) / 1000.0
                extra: dict[str, Any] = {"data_dir": str(data_dir)}
                if title:
                    extra["title"] = title
                if model:
                    extra["model"] = model
                yield SessionFile(
                    source=self.name,
                    path=api_conv,
                    cwd=workspace,
                    session_id=task_dir.name,
                    started_at=started_at,
                    last_modified=stat.st_mtime,
                    extra=extra,
                )

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def _resolve_model(self, session: SessionFile) -> str | None:
        """Best-effort model id: discovery hint, then task_metadata.json."""

        m = session.extra.get("model")
        if isinstance(m, str) and m:
            return m
        meta_path = session.path.with_name("task_metadata.json")
        body = _safe_load_json(meta_path)
        if isinstance(body, dict):
            mu = body.get("model_usage")
            if isinstance(mu, list):
                for row in mu:
                    if isinstance(row, dict):
                        mid = row.get("model_id") or row.get("modelId")
                        if isinstance(mid, str) and mid:
                            return mid
        return None

    def _resolve_usage(self, session: SessionFile) -> dict[str, Any] | None:
        """Sum ``task_metadata.json:model_usage`` into a single usage dict."""

        meta_path = session.path.with_name("task_metadata.json")
        body = _safe_load_json(meta_path)
        if not isinstance(body, dict):
            return None
        return _sum_model_usage(body.get("model_usage"))

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Yield ``(next_index, Record)`` for every message in the canonical stream.

        Cline rewrites ``api_conversation_history.json`` on each turn, so
        the offset is the *array index of the next message to emit* rather
        than a real byte offset. Passing back the value returned from the
        last yield resumes from the following message.
        """

        body = _safe_load_json(session.path)
        if not isinstance(body, list):
            return

        model = self._resolve_model(session)
        usage = self._resolve_usage(session)

        first_assistant_seen = False
        start = max(0, int(start_offset))
        for i in range(start, len(body)):
            msg = body[i]
            if not isinstance(msg, dict):
                continue
            attach_usage: dict[str, Any] | None = None
            if (
                msg.get("role") == "assistant"
                and not first_assistant_seen
                and usage is not None
            ):
                attach_usage = usage
                first_assistant_seen = True
            rec = _project_message_to_record(
                msg,
                session_id=session.session_id,
                cwd=session.cwd,
                index=i,
                model=model,
                attach_usage=attach_usage,
            )
            yield (i + 1, rec)


# Register at import time.
SOURCES.append(ClineSource)


__all__ = ["ClineSource"]
