"""Incremental session → SQLite ingest.

For each session file we:

1. Look up the resume cursor in ``ingest_state`` (or treat as fresh).
2. If the file's inode/size flag a rotation (file shrank, or inode changed),
   reset to offset 0 and wipe any previously-indexed rows from that source.
3. Stream new records via the source's adapter, starting at ``last_offset``.
4. Insert one row per record into ``messages`` (tagged with ``source``);
   run the extractor pipeline over those records and ``INSERT OR IGNORE``
   into ``extractions`` (unique on ``(kind, source_uuid)``).
5. Commit the new cursor (``last_offset``, ``records_seen``, ...) in the same
   transaction as the data rows so a crash never produces "saw the row but
   forgot the offset" drift.

Multi-source (XW8): :func:`ingest_all` accepts an optional ``sources=``
argument; when omitted it iterates every registered :class:`SessionSource`
whose ``is_available()`` returns truthy (Claude Code, Codex, OpenCode,
Gemini CLI, Continue, Cline, Cursor, Aider, ...). When :mod:`lib.sources`
isn't importable (bare branch without XW1) we fall back to the original
Claude-Code-only walker so this module remains independently usable.

Defensive imports: this module is the *one* place index/ depends on the
walker (WT-2) and the extractor pipeline (WT-3). When either is missing on
a bare branch we fall back to no-op stubs so the tests in this slice still
pass and the rest of the pipeline can be validated in isolation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from index.paths import project_key

log = logging.getLogger(__name__)

__all__ = [
    "IngestReport",
    "ingest_file",
    "ingest_all",
]


# ---------------------------------------------------------------------------
# Defensive imports — lib.jsonl_walker (WT-2) and extractors.pipeline (WT-3)
# may not exist on a partial branch. Provide stubs so this module imports.
# ---------------------------------------------------------------------------

try:
    from lib.jsonl_walker import iter_records as _iter_records  # type: ignore[import-not-found]

    _HAS_WALKER = True
except Exception:  # pragma: no cover - exercised only on bare branches
    _HAS_WALKER = False

    def _iter_records(  # type: ignore[no-redef]
        path: Path,
        start_offset: int = 0,
    ) -> Iterator[Any]:
        """Stub walker. Yields nothing — keeps ingest a no-op on bare branches."""
        if False:  # pragma: no cover
            yield None
        return


try:
    from extractors.pipeline import run_all as _run_all  # type: ignore[import-not-found]

    _HAS_PIPELINE = True
except Exception:  # pragma: no cover - exercised only on bare branches
    _HAS_PIPELINE = False

    def _run_all(records: Iterable[Any]) -> Iterator[Any]:  # type: ignore[no-redef]
        """Stub pipeline. Yields no extractions on bare branches."""
        if False:  # pragma: no cover
            yield None
        return


# Defensive import of the secret scrubber. On minimal branches the extractors
# module may be absent — in which case we fall back to a no-op and log ONCE
# per process so the operator knows secrets are NOT being redacted from
# messages.text. Index leakage is a meaningful risk; surface it loudly.
_DEFAULT_PROJECTS_ROOT = Path("~/.claude/projects").expanduser()
_SCRUB_FALLBACK_WARNED = False


def _scrub_fallback(s: Any) -> Any:
    global _SCRUB_FALLBACK_WARNED
    if not _SCRUB_FALLBACK_WARNED:
        log.warning(
            "extractors.secrets not importable; messages.text will NOT be "
            "scrubbed for secrets before insertion into the index"
        )
        _SCRUB_FALLBACK_WARNED = True
    return s


try:
    from extractors.secrets import scrub_secrets as _scrub_secrets  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - exercised only on bare branches
    _scrub_secrets = _scrub_fallback  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class IngestReport:
    """Summary of one file's ingest run. Returned to callers + log-friendly."""

    file: str
    new_messages: int
    new_extractions: int
    errors: int
    elapsed_ms: int
    bytes_processed: int
    new_turns: int = 0
    new_compactions: int = 0
    turn_durations_linked: int = 0


# Module-level flag so we only emit the "metrics tables missing" warning ONCE
# per process. The defensive try/except is necessary because MA1's migration
# adds `turns` / `compactions` / `ingest_runs`; running this ingest module
# against a v1 DB (pre-migration) must not crash.
_METRICS_FALLBACK_WARNED = False


def _warn_metrics_unavailable_once() -> None:
    global _METRICS_FALLBACK_WARNED
    if not _METRICS_FALLBACK_WARNED:
        log.info("metrics tables not yet available; skipping turns/compactions extraction")
        _METRICS_FALLBACK_WARNED = True


def _record_ingest_run(
    conn: sqlite3.Connection,
    reports: list[IngestReport],
    run_t0: float,
    trigger: str,
) -> None:
    """Append one row to `ingest_runs` summarizing this top-level ingest pass.

    Defensive: silently skips when `ingest_runs` doesn't exist (pre-MA1 DB)
    and on any other transient DB error so the caller never crashes solely
    because the summary couldn't be written.
    """
    try:
        elapsed_ms = int((time.monotonic() - run_t0) * 1000)
        files_seen = len(reports)
        files_new = sum(1 for r in reports if r.new_messages > 0)
        sum_messages = sum(r.new_messages for r in reports)
        sum_extractions = sum(r.new_extractions for r in reports)
        sum_turns = sum(getattr(r, "new_turns", 0) for r in reports)
        sum_compactions = sum(getattr(r, "new_compactions", 0) for r in reports)
        sum_errors = sum(r.errors for r in reports)
        ts_int = int(time.time())
        conn.execute(
            """
            INSERT INTO ingest_runs(
                ts, files_seen, files_new, new_messages, new_extractions,
                new_turns, new_compactions, elapsed_ms, errors, trigger
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_int,
                files_seen,
                files_new,
                sum_messages,
                sum_extractions,
                sum_turns,
                sum_compactions,
                elapsed_ms,
                sum_errors,
                trigger,
            ),
        )
    except sqlite3.OperationalError:
        _warn_metrics_unavailable_once()
    except sqlite3.DatabaseError as exc:  # noqa: BLE001
        log.debug("ingest_all: failed to record ingest_runs row: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stat(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_size, int(st.st_mtime)


def _read_state(conn: sqlite3.Connection, source_file: str) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM ingest_state WHERE source_file = ?",
        (source_file,),
    )
    return cur.fetchone()


def _record_text(rec: Any) -> str | None:
    """Best-effort textual projection of a record for FTS5 indexing.

    We DON'T want to index thinking blocks (private). We DO want to index:
    * assistant ``text`` blocks
    * user string content
    * tool-result content (sometimes contains stderr / file paths users grep for)
    * ai-title / last-prompt / system away_summary payloads
    """
    rtype = getattr(rec, "type", None)
    if rtype is None:
        return None

    # ai-title / last-prompt / permission-mode: tiny, useful labels.
    title = getattr(rec, "ai_title", None)
    if isinstance(title, str) and title:
        return title
    last_prompt = getattr(rec, "last_prompt", None)
    if isinstance(last_prompt, str) and last_prompt:
        return last_prompt

    if rtype == "user":
        text = getattr(rec, "text", None)
        if isinstance(text, str) and text.strip():
            return text
        # tool_result content
        tool_results = getattr(rec, "tool_results", None) or []
        parts: list[str] = []
        for tr in tool_results:
            c = getattr(tr, "content", None)
            if isinstance(c, str) and c.strip():
                parts.append(c)
        if parts:
            return "\n".join(parts)
        # last-ditch: stringified content
        content = getattr(rec, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        return None

    if rtype == "assistant":
        blocks = getattr(rec, "content", None) or []
        parts = []
        for b in blocks:
            btype = getattr(b, "type", None)
            if btype == "text":
                t = getattr(b, "text", None)
                if isinstance(t, str) and t.strip():
                    parts.append(t)
            # thinking deliberately excluded.
        if parts:
            return "\n".join(parts)
        return None

    if rtype == "system":
        payload = getattr(rec, "payload", None) or {}
        # away_summary is the high-value subtype — it's a narrative.
        if isinstance(payload, dict):
            for key in ("summary", "text", "content"):
                v = payload.get(key)
                if isinstance(v, str) and v.strip():
                    return v
        return None

    if rtype == "queue-operation":
        content = getattr(rec, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        return None

    return None


def _record_role(rec: Any) -> str:
    rtype = getattr(rec, "type", None) or "?"
    # Map the JSONL `type` to a coarse role for the index.
    if rtype in ("assistant", "user", "system"):
        return rtype
    return rtype  # ai-title / attachment / queue-operation / etc. kept as-is.


def _record_kind(rec: Any) -> str | None:
    """Finer-grained label than `role` — usually the JSONL `type`, or for
    attachment/system records the inner subtype/attachment_type."""
    rtype = getattr(rec, "type", None)
    if rtype == "attachment":
        return getattr(rec, "attachment_type", None) or "attachment"
    if rtype == "system":
        return getattr(rec, "subtype", None) or "system"
    return rtype


def _ts_int(rec: Any) -> int | None:
    ts = getattr(rec, "ts", None)
    if isinstance(ts, datetime):
        return int(ts.timestamp())
    return None


def _row_for_message(rec: Any, source_file: str, source: str = "claude_code") -> tuple:
    text = _record_text(rec)
    # Scrub secrets BEFORE the row goes into messages.text. The MCP
    # `search_messages` tool returns raw transcript text and would otherwise
    # happily leak keys / tokens that the user pasted into Claude Code.
    if isinstance(text, str) and text:
        text = _scrub_secrets(text)
    return (
        getattr(rec, "session_id", None) or "",
        getattr(rec, "cwd", None),
        getattr(rec, "git_branch", None),
        _record_role(rec),
        _record_kind(rec),
        _ts_int(rec),
        getattr(rec, "parent_uuid", None),
        getattr(rec, "uuid", None),
        int(getattr(rec, "byte_offset", 0) or 0),
        source_file,
        text,
        None,  # raw_json opt-in; default off saves ~5x disk.
        source,  # v4: which CLI client this row came from.
        project_key(getattr(rec, "cwd", None)),  # v5: worktree-collapsed root.
    )


def _row_for_turn(rec: Any, source_file: str) -> tuple | None:
    """Build a `turns` row from an assistant record carrying `message.usage`.

    Returns None if the record is not an assistant record or has no usage
    block (e.g. assistant tool-only chunks that lack billing info).
    """
    if getattr(rec, "type", None) != "assistant":
        return None
    raw = getattr(rec, "raw", None) or {}
    msg = raw.get("message") if isinstance(raw, dict) else None
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    ts_val = getattr(rec, "ts", None)
    ts_int = int(ts_val.timestamp()) if isinstance(ts_val, datetime) else None

    return (
        getattr(rec, "session_id", None) or "",
        getattr(rec, "cwd", None),
        ts_int,
        msg.get("model"),
        usage.get("input_tokens"),
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("output_tokens"),
        None,  # duration_ms — linked from sibling system.turn_duration; out of scope
        msg.get("stop_reason"),
        raw.get("requestId"),
        getattr(rec, "uuid", None),
        source_file,
    )


def _row_for_compaction(rec: Any, source_file: str) -> tuple | None:
    """Build a `compactions` row from a `system.subtype == 'compact_boundary'`."""
    if getattr(rec, "type", None) != "system":
        return None
    if getattr(rec, "subtype", None) != "compact_boundary":
        return None
    payload = getattr(rec, "payload", None) or {}
    cm = payload.get("compactMetadata", {}) if isinstance(payload, dict) else {}
    if not isinstance(cm, dict):
        cm = {}
    ts_val = getattr(rec, "ts", None)
    ts_int = int(ts_val.timestamp()) if isinstance(ts_val, datetime) else None
    return (
        getattr(rec, "session_id", None) or "",
        getattr(rec, "cwd", None),
        ts_int,
        cm.get("preTokens"),
        cm.get("postTokens"),
        cm.get("durationMs"),
        cm.get("trigger"),
        getattr(rec, "uuid", None),
        source_file,
    )


def _profile_record_snapshot(rec: Any) -> dict | None:
    """Build a tiny dict carrying just what the incremental profile updaters need.

    Returns ``None`` for record types that have no text payload worth
    scanning (attachments, queue ops, tool_use blocks). The shape mirrors
    what :func:`extractors.voice_profile._user_string_text` expects so the
    voice extractor can ingest these snapshots without rewrap.
    """
    rtype = getattr(rec, "type", None)
    if rtype not in ("user", "assistant", "system"):
        return None

    snap: dict = {"type": rtype}

    if rtype == "user":
        text = getattr(rec, "text", None)
        snap["content_kind"] = getattr(rec, "content_kind", None) or "empty"
        if isinstance(text, str):
            snap["text"] = text
        return snap

    if rtype == "assistant":
        parts: list[str] = []
        for b in getattr(rec, "content", None) or []:
            if getattr(b, "type", None) == "text":
                bt = getattr(b, "text", None)
                if isinstance(bt, str) and bt:
                    parts.append(bt)
        if not parts:
            return None
        snap["text"] = "\n".join(parts)
        return snap

    # system: only away_summary / similar narrative payloads carry text.
    payload = getattr(rec, "payload", None) or {}
    if isinstance(payload, dict):
        for key in ("summary", "text", "content"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                snap["text"] = v
                return snap
    return None


def _row_for_extraction(ext: Any, source: str = "claude_code") -> tuple:
    ts = getattr(ext, "ts", None)
    ts_int = int(ts.timestamp()) if isinstance(ts, datetime) else None
    context = getattr(ext, "context", None) or {}
    try:
        ctx_json = json.dumps(context, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        ctx_json = None
    return (
        getattr(ext, "kind", "?"),
        getattr(ext, "content", "") or "",
        getattr(ext, "session_id", "") or "",
        getattr(ext, "cwd", None),
        ts_int,
        getattr(ext, "source_uuid", None),
        float(getattr(ext, "score", 0.5) or 0.5),
        getattr(ext, "scope", "project") or "project",
        ctx_json,
        source,  # v4: which CLI client produced the source record.
        project_key(getattr(ext, "cwd", None)),  # v5: worktree-collapsed root.
    )


# ---------------------------------------------------------------------------
# Parse-only path (used by both sequential ingest_file and the parallel pool)
# ---------------------------------------------------------------------------


@dataclass
class _ParsedFile:
    """Result of parsing one JSONL file with no DB side effects.

    Fields are intentionally all simple types (str / int / list[tuple] / None)
    so this dataclass is cheap to serialize across a multiprocessing boundary.
    The `*_rows` lists hold the exact tuples that the sequential commit path
    would `executemany` into their respective tables.
    """

    source_file: str
    inode: int
    size: int
    mtime: int
    rotated: bool
    start_offset: int
    message_rows: list[tuple] = field(default_factory=list)
    turn_rows: list[tuple] = field(default_factory=list)
    compaction_rows: list[tuple] = field(default_factory=list)
    extraction_rows: list[tuple] = field(default_factory=list)
    # (parent_uuid, duration_ms) pairs for system.turn_duration -> turns linkage
    turn_duration_links: list[tuple[str, int]] = field(default_factory=list)
    last_session_id: str | None = None
    errors: int = 0
    # Set when the source file cannot be stat'd (deleted / permission error).
    # Callers should turn this into an IngestReport with errors=1 and skip.
    missing: bool = False
    # Lightweight per-record snapshots ({type, content_kind, text}) for the
    # incremental-profile updaters in _commit_parsed. Built during parse so
    # the data survives the worker/serializer boundary. Kept tiny (no
    # nested objects) for cheap pickling.
    profile_records: list[dict] = field(default_factory=list)
    # v4: which CLI-client adapter produced this file ('claude_code',
    # 'opencode', 'codex', ...). Threaded through so _commit_parsed can
    # stamp every inserted row.
    source: str = "claude_code"


def _parse_file_pure(
    jsonl_path: Path,
    start_offset: int = 0,
    rotated: bool = False,
    last_session_id: str | None = None,
    source: str = "claude_code",
    source_file_key: str | None = None,
    record_iter: Any = None,
) -> _ParsedFile:
    """Pure parse + extract pass over one session file. No DB connection.

    This is what worker processes call. The result is a `_ParsedFile`
    containing every row tuple that the sequential commit half of ingest
    would have inserted. Designed to be called from a `ProcessPoolExecutor`.

    `start_offset` and `rotated` come from a pre-read of `ingest_state` in
    the main process; workers do not touch the DB.

    ``source`` tags each emitted row with the originating CLI client; it
    defaults to ``"claude_code"`` so legacy callers keep working.

    ``source_file_key`` overrides the value stored in ``ingest_state``
    and ``messages.source_file``. The default (``None``) keys on the
    file path, which is correct for one-file-per-session sources
    (Claude Code, Codex, Aider). SQLite-backed sources (OpenCode,
    Cursor) pack many sessions into one DB and must pass
    ``f"{db_path}#{session_id}"`` so each session gets its own resume
    cursor.

    ``record_iter`` lets multi-source callers supply a pre-built
    iterator (returned from ``SessionSource.iter_records(session)``).
    When ``None`` we fall back to the legacy Claude-Code walker for
    full back-compat.
    """
    jsonl_path = Path(jsonl_path)
    source_file = source_file_key if source_file_key is not None else str(jsonl_path)

    st = _stat(jsonl_path)
    if st is None:
        # File on disk is gone. For the legacy Claude-Code path
        # (record_iter is None) that's a hard error — the walker can't
        # do anything without it. For multi-source callers who supplied
        # their own record_iter (e.g. OpenCode pulling from SQLite where
        # the "session path" may not literally exist on disk), we
        # synthesize a zero-stat so the cursor stays sane.
        if record_iter is None:
            return _ParsedFile(
                source_file=source_file,
                inode=0,
                size=0,
                mtime=0,
                rotated=False,
                start_offset=0,
                missing=True,
                source=source,
            )
        inode, size, mtime = 0, 0, 0
    else:
        inode, size, mtime = st

    # If we're treating the file as rotated, the caller will DELETE existing
    # rows in the commit step; here we just start from offset 0.
    effective_start = 0 if rotated else start_offset

    parsed = _ParsedFile(
        source_file=source_file,
        inode=inode,
        size=size,
        mtime=mtime,
        rotated=rotated,
        start_offset=effective_start,
        last_session_id=last_session_id,
        source=source,
    )

    # Pick the right record iterator. Callers passing a `record_iter` win;
    # otherwise we use the legacy JSONL walker (Claude Code) and bail out
    # cleanly if it's unavailable on a bare branch.
    if record_iter is None:
        if not _HAS_WALKER:
            return parsed
        record_iter = _iter_records(jsonl_path, start_offset=effective_start)

    records: list[Any] = []
    for _offset, rec in record_iter:
        try:
            records.append(rec)
            parsed.message_rows.append(_row_for_message(rec, source_file, source=source))
            # Per-record snapshot for the incremental profile updaters.
            # We capture the minimum the three extractors need (no nested
            # dataclasses → cheap to pickle across the worker boundary).
            _snap = _profile_record_snapshot(rec)
            if _snap is not None:
                parsed.profile_records.append(_snap)
            parsed.last_session_id = getattr(rec, "session_id", None) or parsed.last_session_id
            trow = _row_for_turn(rec, source_file)
            if trow is not None:
                parsed.turn_rows.append(trow)
            crow = _row_for_compaction(rec, source_file)
            if crow is not None:
                parsed.compaction_rows.append(crow)
            if (
                getattr(rec, "type", None) == "system"
                and getattr(rec, "subtype", None) == "turn_duration"
            ):
                payload = getattr(rec, "payload", None) or {}
                if isinstance(payload, dict):
                    dur = payload.get("durationMs")
                    parent = getattr(rec, "parent_uuid", None)
                    if dur is not None and parent:
                        parsed.turn_duration_links.append((parent, int(dur)))
        except Exception as exc:  # noqa: BLE001 - log + continue
            log.warning("_parse_file_pure: skipping record: %s", exc)
            parsed.errors += 1

    if _HAS_PIPELINE and records:
        try:
            for ext in _run_all(records):
                parsed.extraction_rows.append(_row_for_extraction(ext, source=source))
        except Exception as exc:  # noqa: BLE001
            log.warning("_parse_file_pure: extractor pipeline error: %s", exc)
            parsed.errors += 1

    return parsed


def _parse_worker(args: tuple) -> _ParsedFile:
    """Top-level entrypoint for ProcessPoolExecutor workers.

    Wraps `_parse_file_pure` in a broad try/except so a malformed file in
    one worker can't take down the whole pool — the failure surfaces as a
    `_ParsedFile` with `errors >= 1` and no row data.

    Args tuple is ``(path, start_offset, rotated, last_session_id, source?)``.
    The trailing ``source`` is optional for back-compat with single-source
    callers — falls back to ``"claude_code"``.
    """
    # Tuple is variable-length to keep back-compat with v3 callers (4-tuple).
    path_str = args[0]
    start_offset = args[1]
    rotated = args[2]
    last_session_id = args[3]
    source = args[4] if len(args) >= 5 else "claude_code"
    try:
        return _parse_file_pure(
            Path(path_str),
            start_offset=start_offset,
            rotated=rotated,
            last_session_id=last_session_id,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001 - worker isolation
        log.warning("_parse_worker(%s): %s", path_str, exc)
        st = _stat(Path(path_str))
        if st is None:
            return _ParsedFile(
                source_file=path_str,
                inode=0,
                size=0,
                mtime=0,
                rotated=False,
                start_offset=0,
                errors=1,
                missing=True,
                source=source,
            )
        inode, size, mtime = st
        return _ParsedFile(
            source_file=path_str,
            inode=inode,
            size=size,
            mtime=mtime,
            rotated=rotated,
            start_offset=start_offset,
            errors=1,
            last_session_id=last_session_id,
            source=source,
        )


def _existing_profile_from_conn(conn: sqlite3.Connection) -> Any:
    """Reconstruct an :class:`OperatorProfile` from the stored row set.

    Returns ``None`` when the table is empty / missing. Defensive — any DB
    error degrades to ``None`` so the incremental updater treats this as a
    cold start.
    """
    try:
        from extractors.operator_profile import OperatorProfile
        from index.operator import get_profile
    except Exception:  # pragma: no cover - missing sibling module
        return None
    try:
        stored = get_profile(conn)
    except sqlite3.DatabaseError:
        return None
    if not stored or set(stored.keys()) <= {"_confidence", "_sources"}:
        return None
    profile = OperatorProfile()
    for key, val in stored.items():
        if key.startswith("_"):
            continue
        if hasattr(profile, key):
            try:
                setattr(profile, key, val)
            except Exception:  # noqa: BLE001
                continue
    profile.confidence = dict(stored.get("_confidence", {}) or {})
    profile.sources = {k: list(v) for k, v in (stored.get("_sources", {}) or {}).items()}
    return profile


def _existing_voice_from_conn(conn: sqlite3.Connection) -> dict | None:
    """Return the stored voice profile as a dict, or ``None`` if empty."""
    try:
        from index.voice import get_voice
    except Exception:  # pragma: no cover
        return None
    try:
        stored = get_voice(conn)
    except sqlite3.DatabaseError:
        return None
    if not stored or set(stored.keys()) <= {"_measured_at", "_sample_size"}:
        return None
    return stored


def _max_rowid(conn: sqlite3.Connection, table: str) -> int:
    """Highest INTEGER PRIMARY KEY in ``table``, or 0. Cheap via PK."""
    try:
        row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _new_rows_since(conn: sqlite3.Connection, table: str, before_id: int) -> int:
    """Count rows with ``id > before_id`` (PK range scan, not full-table)."""
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE id > ?",
            (before_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0] or 0) if row else 0


def _commit_parsed(
    conn: sqlite3.Connection,
    parsed: _ParsedFile,
    *,
    update_profiles: bool = True,
) -> IngestReport:
    """Write a `_ParsedFile`'s rows into the DB inside a single transaction.

    This is the commit half of `ingest_file` — extracted so the parallel
    path can reuse it. Mirrors the sequential commit logic exactly:
    rotation wipes old rows, INSERT OR IGNORE everywhere, idempotent
    cursor update, defensive try/except around the v2 metrics tables.

    ``update_profiles=False`` skips per-file incremental profile work
    (rebuild bulk path does a single cold consolidation pass instead).
    New-row counts use PK-range ``id > max_before`` — not full-table
    ``COUNT(*)`` and not ``total_changes`` (FTS triggers inflate the latter).
    """
    t0 = time.monotonic()
    source_file = parsed.source_file

    if parsed.missing:
        return IngestReport(source_file, 0, 0, 1, 0, 0)

    new_messages = 0
    new_extractions = 0
    new_turns = 0
    new_compactions = 0
    turn_durations_linked = 0
    errors = parsed.errors

    state = _read_state(conn, source_file)

    conn.execute("BEGIN")
    try:
        if parsed.rotated:
            conn.execute(
                "DELETE FROM messages WHERE source_file = ?",
                (source_file,),
            )

        if parsed.message_rows:
            # v4: append the `source` column so the row carries its origin
            # CLI client. _row_for_message already produces the trailing
            # `source` value. Defensive: if the column hasn't been migrated
            # in (running against a pre-v4 DB someone bypassed apply_schema),
            # retry against the v3 column list.
            before_id = _max_rowid(conn, "messages")
            try:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO messages(
                        session_id, cwd, git_branch, role, kind, ts,
                        parent_uuid, message_uuid, byte_offset, source_file,
                        text, raw_json, source, project_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    parsed.message_rows,
                )
            except sqlite3.OperationalError as exc:
                if "project_key" in str(exc):
                    log.warning(
                        "messages.project_key column missing; falling back to "
                        "v4 INSERT shape — run apply_schema() to enable "
                        "project pooling"
                    )
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO messages(
                            session_id, cwd, git_branch, role, kind, ts,
                            parent_uuid, message_uuid, byte_offset, source_file,
                            text, raw_json, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        # Strip the trailing project_key to match the v4 shape.
                        [row[:-1] for row in parsed.message_rows],
                    )
                elif "source" in str(exc):
                    log.warning(
                        "messages.source column missing; falling back to "
                        "v3 INSERT shape — run apply_schema() to enable "
                        "multi-source ingest"
                    )
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO messages(
                            session_id, cwd, git_branch, role, kind, ts,
                            parent_uuid, message_uuid, byte_offset, source_file,
                            text, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        # Strip the trailing project_key + source (v3 shape).
                        [row[:-2] for row in parsed.message_rows],
                    )
                else:
                    raise
            new_messages = _new_rows_since(conn, "messages", before_id)

        if parsed.turn_rows:
            try:
                before_t = _max_rowid(conn, "turns")
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO turns(
                        session_id, cwd, ts, model, input_tokens,
                        cache_creation_tokens, cache_read_tokens,
                        output_tokens, duration_ms, stop_reason,
                        request_id, message_uuid, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    parsed.turn_rows,
                )
                new_turns = _new_rows_since(conn, "turns", before_t)
            except sqlite3.OperationalError:
                _warn_metrics_unavailable_once()
                new_turns = 0

        if parsed.turn_duration_links:
            try:
                cur = conn.executemany(
                    "UPDATE turns SET duration_ms = ? "
                    "WHERE message_uuid = ? AND duration_ms IS NULL",
                    [(dur, parent) for (parent, dur) in parsed.turn_duration_links],
                )
                linked = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                turn_durations_linked = linked
            except sqlite3.OperationalError:
                _warn_metrics_unavailable_once()
                turn_durations_linked = 0

        if parsed.compaction_rows:
            try:
                before_c = _max_rowid(conn, "compactions")
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO compactions(
                        session_id, cwd, ts, pre_tokens, post_tokens,
                        duration_ms, trigger, message_uuid, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    parsed.compaction_rows,
                )
                new_compactions = _new_rows_since(conn, "compactions", before_c)
            except sqlite3.OperationalError:
                _warn_metrics_unavailable_once()
                new_compactions = 0

        if parsed.extraction_rows:
            before_e = _max_rowid(conn, "extractions")
            try:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO extractions(
                        kind, content, session_id, cwd, ts, source_uuid,
                        score, scope, context_json, source, project_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    parsed.extraction_rows,
                )
            except sqlite3.OperationalError as exc:
                if "project_key" in str(exc):
                    log.warning(
                        "extractions.project_key column missing; falling back to v4 INSERT shape"
                    )
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO extractions(
                            kind, content, session_id, cwd, ts, source_uuid,
                            score, scope, context_json, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [row[:-1] for row in parsed.extraction_rows],
                    )
                elif "source" in str(exc):
                    log.warning(
                        "extractions.source column missing; falling back to v3 INSERT shape"
                    )
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO extractions(
                            kind, content, session_id, cwd, ts, source_uuid,
                            score, scope, context_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [row[:-2] for row in parsed.extraction_rows],
                    )
                else:
                    raise
            new_extractions = _new_rows_since(conn, "extractions", before_e)

        new_offset = parsed.size
        bytes_processed = max(0, new_offset - parsed.start_offset)
        records_seen_delta = new_messages

        if state is None or parsed.rotated:
            conn.execute(
                """
                INSERT OR REPLACE INTO ingest_state(
                    source_file, inode, size, mtime, last_offset,
                    last_session_id, records_seen, errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_file,
                    parsed.inode,
                    parsed.size,
                    parsed.mtime,
                    new_offset,
                    parsed.last_session_id,
                    records_seen_delta,
                    errors,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE ingest_state
                   SET inode = ?, size = ?, mtime = ?, last_offset = ?,
                       last_session_id = COALESCE(?, last_session_id),
                       records_seen = records_seen + ?,
                       errors = errors + ?
                 WHERE source_file = ?
                """,
                (
                    parsed.inode,
                    parsed.size,
                    parsed.mtime,
                    new_offset,
                    parsed.last_session_id,
                    records_seen_delta,
                    errors,
                    source_file,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Incremental profile updates (cheap hot path per R2 research).
    # Wrapped in try/except — these are best-effort and must never fail
    # the ingest. Each updater is responsible for its own persistence so
    # we can run them independently and let one crash without losing the
    # others. Runs only when there are new records to score against.
    # Rebuild bulk_load skips this — cmd_rebuild does one cold consolidation.
    if update_profiles and parsed.profile_records:
        try:
            from extractors.ontology import (
                update_vocabulary_counts as _o_inc,
            )
            from extractors.operator_profile import (
                extract_incremental as _op_inc,
            )
            from extractors.operator_profile import (
                persist_incremental_profile as _op_persist,
            )
            from extractors.voice_profile import (
                measure_voice_incremental as _v_inc,
            )
            from index.voice import persist_voice_profile

            _new_records = parsed.profile_records

            # Operator profile — append-supersede merge with the stored row.
            try:
                _existing_op = _existing_profile_from_conn(conn)
                _merged_op = _op_inc(_new_records, _existing_op)
                _op_persist(conn, _merged_op)
            except Exception as exc:  # noqa: BLE001
                log.warning("operator_profile incremental update failed: %s", exc)

            # Voice profile — EMA blend over the rolling window.
            try:
                _existing_v = _existing_voice_from_conn(conn)
                _merged_v = _v_inc(_new_records, _existing_v)
                if _merged_v:
                    persist_voice_profile(
                        conn,
                        _merged_v,
                        sample_size=_merged_v.get("sample_size"),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("voice_profile incremental update failed: %s", exc)

            # Vocabulary frequency bumps.
            try:
                _o_inc(_new_records, conn)
            except Exception as exc:  # noqa: BLE001
                log.warning("vocabulary incremental update failed: %s", exc)

            # WorkflowProfile (B1) — EMA-blended over rolling session window.
            try:
                from extractors.workflow import extract_workflow_incremental as _wf_inc
                from index.workflow import get_workflow, persist_workflow

                _existing_wf = get_workflow(conn) or {}
                _existing_wf.pop("_updated_ts", None)
                _merged_wf = _wf_inc(_new_records, _existing_wf)
                if _merged_wf:
                    persist_workflow(conn, _merged_wf)
            except Exception as exc:  # noqa: BLE001
                log.warning("workflow incremental update failed: %s", exc)

            # ImplicitPreferenceProfile (B2) — evidence-accumulating merge.
            # The incremental extractor maintains accumulated counts inside the
            # batch itself; the existing persisted rows can stay (persist is
            # idempotent). Pass None so the extractor builds a fresh batch
            # candidate which is then upsert-merged into the table.
            try:
                from extractors.implicit_preferences import (
                    extract_implicit_preferences_incremental as _ip_inc,
                )
                from index.implicit_preferences import persist_implicit_preferences

                _merged_ip = _ip_inc(_new_records, None)
                if _merged_ip:
                    persist_implicit_preferences(conn, _merged_ip)
            except Exception as exc:  # noqa: BLE001
                log.warning("implicit_preferences incremental update failed: %s", exc)

            # SatisfactionProfile (B4) — additive matrix accumulation.
            try:
                from extractors.satisfaction import (
                    extract_satisfaction_incremental as _sat_inc,
                )
                from index.satisfaction import (
                    get_satisfaction_summary,
                    persist_satisfaction,
                )

                _existing_sat = get_satisfaction_summary(conn)
                _merged_sat = _sat_inc(_new_records, _existing_sat)
                if _merged_sat:
                    persist_satisfaction(conn, _merged_sat)
            except Exception as exc:  # noqa: BLE001
                log.warning("satisfaction incremental update failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("incremental profile updates failed (non-fatal): %s", exc)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return IngestReport(
        file=source_file,
        new_messages=new_messages,
        new_extractions=new_extractions,
        errors=errors,
        elapsed_ms=elapsed_ms,
        bytes_processed=bytes_processed,
        new_turns=new_turns,
        new_compactions=new_compactions,
        turn_durations_linked=turn_durations_linked,
    )


def _resolve_cursor(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    force_full: bool = False,
) -> tuple[int, bool, str | None]:
    """Read ingest_state for this path and decide (start_offset, rotated, last_sid).

    Mirrors the rotation logic that used to live inline in `ingest_file` so
    both sequential and parallel paths share one source of truth.
    """
    source_file = str(jsonl_path)
    st = _stat(jsonl_path)
    if st is None:
        return 0, False, None
    inode, size, _mtime = st

    state = _read_state(conn, source_file)
    if state is None or force_full:
        return 0, force_full, (state["last_session_id"] if state else None)

    prev_inode = state["inode"]
    prev_size = state["size"] or 0
    if prev_inode is not None and prev_inode != inode:
        return 0, True, state["last_session_id"]
    if size < prev_size:
        # File shrank — either truncated or replaced; safest to re-ingest.
        return 0, True, state["last_session_id"]
    return int(state["last_offset"] or 0), False, state["last_session_id"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_file(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    force_full: bool = False,
) -> IngestReport:
    """Ingest one ``.jsonl`` session file incrementally.

    Resumes from ``ingest_state.last_offset`` unless ``force_full=True`` or
    the file's inode has changed (rotation). All inserts + the state row
    update happen in a single transaction so a crash leaves the DB
    consistent.

    Implementation note: this function is a thin wrapper around the
    ``_parse_file_pure`` + ``_commit_parsed`` split. The split exists so the
    parallel path in :func:`ingest_all` can fan parsing out to a process
    pool and funnel writes back through a single SQLite connection.
    """
    jsonl_path = Path(jsonl_path)
    start_offset, rotated, last_sid = _resolve_cursor(conn, jsonl_path, force_full=force_full)
    parsed = _parse_file_pure(
        jsonl_path,
        start_offset=start_offset,
        rotated=rotated,
        last_session_id=last_sid,
    )
    return _commit_parsed(conn, parsed)


def _discover_jsonl_files(projects_root: Path, cwd_filter: str | None) -> list[Path]:
    """Walk projects_root and return the top-level .jsonl files to ingest.

    Plain subagent transcripts at ``<slug>/<uuid>/subagents/*.jsonl`` are
    excluded by design (glob is non-recursive) — the parent session's
    ``user`` record already carries the agent's summarized tool_result.

    ``Workflow``-tool subagent transcripts at
    ``<slug>/<uuid>/subagents/workflows/wf_*/agent-*.jsonl`` ARE included:
    unlike a plain subagent, a workflow agent's full reasoning is not
    otherwise summarized anywhere in the parent transcript, so it would be
    permanently unsearchable if skipped. Their records carry their own
    ``sessionId``/``cwd`` (same as the parent session), so they join the
    existing session rather than creating a new one. The sibling
    ``journal.jsonl`` per ``wf_*`` dir is still excluded — it carries no
    ``sessionId``/``cwd`` and its ``result`` text duplicates the closing
    assistant turn already present in ``agent-*.jsonl``.

    Output is sorted (by slug then file) so worker scheduling is
    deterministic.
    """
    files: list[Path] = []
    if not projects_root.exists():
        return files
    for slug_dir in sorted(projects_root.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        if cwd_filter:
            expected_slug = cwd_filter.replace("/", "-")
            if slug_dir.name != expected_slug:
                continue
        for path in sorted(slug_dir.glob("*.jsonl")):
            files.append(path)
            workflows_dir = path.with_suffix("") / "subagents" / "workflows"
            if not workflows_dir.is_dir():
                continue
            files.extend(sorted(workflows_dir.glob("wf_*/agent-*.jsonl")))
    return files


def _source_file_key_for_session(session: Any) -> str:
    """Build the ``ingest_state.source_file`` key for a SessionFile.

    File-per-session sources (Claude Code, Codex, Aider, Gemini CLI) key
    on the absolute path. SQLite-backed sources (OpenCode, Cursor) pack
    many sessions into one DB and must disambiguate with
    ``f"{db_path}#{session_id}"`` so each session gets its own resume
    cursor. We detect SQLite-backed sources via ``extra['storage'] ==
    'sqlite'`` (the convention used by ``lib.sources.opencode``).
    """
    extra = getattr(session, "extra", None) or {}
    storage = extra.get("storage") if isinstance(extra, dict) else None
    path_str = str(getattr(session, "path", ""))
    if storage == "sqlite":
        sid = getattr(session, "session_id", "") or ""
        return f"{path_str}#{sid}"
    return path_str


def _session_is_poolable(session: Any) -> bool:
    """True when a session can be parsed by :func:`_parse_worker` (path only).

    File-per-session JSONL sources (Claude Code, Codex, …) qualify when the
    path exists on disk. SQLite-backed sources (OpenCode, Cursor) pack many
    sessions into one DB and need ``iter_records`` from the adapter — those
    stay on the serial path (not picklable into a process pool).
    """
    extra = getattr(session, "extra", None) or {}
    if isinstance(extra, dict) and extra.get("storage") == "sqlite":
        return False
    path = Path(getattr(session, "path", "") or "")
    return path.is_file()


def _dedup_and_commit_parsed(
    conn: sqlite3.Connection,
    parsed: _ParsedFile,
    *,
    src_name: str,
    source_file_key: str,
    msg_seen: dict[tuple[str, int, str], str],
    ext_seen: dict[tuple[str, int, str], str],
    msg_cwd_idx: int,
    msg_ts_idx: int,
    msg_text_idx: int,
    ext_cwd_idx: int,
    ext_ts_idx: int,
    ext_text_idx: int,
    update_profiles: bool = True,
) -> IngestReport:
    """Apply cross-source dedup then commit one parsed session."""
    from index.multi_source import filter_dedup_rows  # noqa: WPS433

    if parsed.message_rows:
        kept, _seen_m, suppressed = filter_dedup_rows(
            parsed.message_rows,
            source=src_name,
            cwd_idx=msg_cwd_idx,
            ts_idx=msg_ts_idx,
            text_idx=msg_text_idx,
            seen=msg_seen,  # mutated in place; same object returned
        )
        if suppressed:
            log.info(
                "ingest_all: dedup suppressed %d %s messages",
                suppressed,
                src_name,
            )
        parsed.message_rows = kept
    if parsed.extraction_rows:
        kept_e, _seen_e, suppressed_e = filter_dedup_rows(
            parsed.extraction_rows,
            source=src_name,
            cwd_idx=ext_cwd_idx,
            ts_idx=ext_ts_idx,
            text_idx=ext_text_idx,
            seen=ext_seen,
        )
        if suppressed_e:
            log.info(
                "ingest_all: dedup suppressed %d %s extractions",
                suppressed_e,
                src_name,
            )
        parsed.extraction_rows = kept_e

    try:
        return _commit_parsed(conn, parsed, update_profiles=update_profiles)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ingest_all: commit for %s/%s failed: %s",
            src_name,
            source_file_key,
            exc,
        )
        return IngestReport(source_file_key, 0, 0, 1, 0, 0)


def _ingest_all_multi_source(
    *,
    conn: sqlite3.Connection,
    active: list[Any],
    cwd_filter: str | None,
    force_full: bool,
    jobs: int = 1,
    update_profiles: bool = True,
) -> list[IngestReport]:
    """Multi-source ingest loop with cross-source dedup applied at commit time.

    Iterates sources in priority order (the order returned by
    :func:`_available_sources`). Within each source, when ``jobs > 1`` and
    sessions are file-backed (poolable), parse fans out via
    :class:`concurrent.futures.ProcessPoolExecutor` — same model as the
    legacy claude_code path. SQLite-backed adapters stay serial.

    After parsing, the :mod:`index.multi_source` dedup heuristic prunes
    message rows whose ``(cwd, minute_bucket, sha256(text[:200]))`` triple
    already lives in a higher-priority source's batch within this same
    ingest pass. Dedup + SQLite commit stay on the main process.

    Best-effort: dedup state is per-call (an in-memory dict), so a
    duplicate that splits across separate ingest runs WILL land in the
    DB. The ``source`` column lets downstream queries collapse those at
    query time if needed.
    """
    import concurrent.futures as cf

    reports: list[IngestReport] = []
    # message_rows tuple layout (see _row_for_message):
    #   0 session_id, 1 cwd, 2 git_branch, 3 role, 4 kind, 5 ts,
    #   6 parent_uuid, 7 message_uuid, 8 byte_offset, 9 source_file,
    #   10 text, 11 raw_json, 12 source
    msg_cwd_idx, msg_ts_idx, msg_text_idx = 1, 5, 10
    # extraction_rows tuple layout (see _row_for_extraction):
    #   0 kind, 1 content, 2 session_id, 3 cwd, 4 ts, 5 source_uuid,
    #   6 score, 7 scope, 8 context_json, 9 source
    ext_cwd_idx, ext_ts_idx, ext_text_idx = 3, 4, 1

    msg_seen: dict[tuple[str, int, str], str] = {}
    ext_seen: dict[tuple[str, int, str], str] = {}
    job_count = max(1, int(jobs))

    for src in active:
        src_name = getattr(src, "name", "?")
        try:
            sessions = list(src.discover_sessions())
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ingest_all: %s.discover_sessions raised %s — skipping",
                src_name,
                exc,
            )
            continue

        if cwd_filter:
            sessions = [
                s
                for s in sessions
                if getattr(s, "cwd", None) == cwd_filter
                or getattr(s, "cwd", None) == cwd_filter.rstrip("/")
            ]

        # Split poolable (file-on-disk) vs serial (sqlite / synthetic / missing).
        pool_jobs: list[tuple] = []
        pool_keys: list[str] = []
        serial_items: list[tuple[Any, str, int, bool, Any]] = []

        for session in sessions:
            source_file_key = _source_file_key_for_session(session)
            session_path = Path(getattr(session, "path", source_file_key))
            state = _read_state(conn, source_file_key)
            if state is None or force_full:
                start_offset = 0
                rotated = bool(force_full)
                last_sid = state["last_session_id"] if state else None
            else:
                start_offset = int(state["last_offset"] or 0)
                rotated = False
                last_sid = state["last_session_id"]

            if job_count > 1 and _session_is_poolable(session):
                # Path-only worker — same 5-tuple as legacy parallel path.
                pool_jobs.append((str(session_path), start_offset, rotated, last_sid, src_name))
                pool_keys.append(source_file_key)
            else:
                serial_items.append((session, source_file_key, start_offset, rotated, last_sid))

        # --- parallel parse (file-backed sessions of this source) ----------
        # Match the legacy claude_code path: submit + as_completed so up to
        # job_count worker *processes* stay busy while main commits each
        # finished parse (SQLite single-writer). pool.map() was wrong here —
        # it waited for ALL parses before ANY commit, so after the parse
        # burst the process tree showed 1 main thread and zero workers for
        # the long SQLite phase (jobs=N looked like a lie).
        if pool_jobs:
            workers = min(job_count, len(pool_jobs))
            log.info(
                "ingest_all: multi-source parallel parse source=%s files=%d jobs=%d",
                src_name,
                len(pool_jobs),
                workers,
            )
            with cf.ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_parse_worker, args): key
                    for args, key in zip(pool_jobs, pool_keys, strict=True)
                }
                for fut in cf.as_completed(futures):
                    source_file_key = futures[fut]
                    try:
                        parsed = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "ingest_all: worker for %s/%s failed: %s",
                            src_name,
                            source_file_key,
                            exc,
                        )
                        reports.append(IngestReport(source_file_key, 0, 0, 1, 0, 0))
                        continue
                    # Pool workers key source_file on path; multi-source may
                    # need the #session_id form for sqlite (not poolable) —
                    # for files path == key. Force the key we resolved.
                    if parsed.source_file != source_file_key:
                        parsed.source_file = source_file_key
                    try:
                        reports.append(
                            _dedup_and_commit_parsed(
                                conn,
                                parsed,
                                src_name=src_name,
                                source_file_key=source_file_key,
                                msg_seen=msg_seen,
                                ext_seen=ext_seen,
                                msg_cwd_idx=msg_cwd_idx,
                                msg_ts_idx=msg_ts_idx,
                                msg_text_idx=msg_text_idx,
                                ext_cwd_idx=ext_cwd_idx,
                                ext_ts_idx=ext_ts_idx,
                                ext_text_idx=ext_text_idx,
                                update_profiles=update_profiles,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "ingest_all: commit for %s/%s failed: %s",
                            src_name,
                            source_file_key,
                            exc,
                        )
                        reports.append(IngestReport(source_file_key, 0, 0, 1, 0, 0))

        # --- serial parse (sqlite / non-file adapters) ---------------------
        for session, source_file_key, start_offset, rotated, last_sid in serial_items:
            session_path = Path(getattr(session, "path", source_file_key))
            try:
                record_iter = src.iter_records(session, start_offset=start_offset)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ingest_all: %s.iter_records(%s) raised %s",
                    src_name,
                    source_file_key,
                    exc,
                )
                reports.append(IngestReport(source_file_key, 0, 0, 1, 0, 0))
                continue

            try:
                parsed = _parse_file_pure(
                    session_path,
                    start_offset=start_offset,
                    rotated=rotated,
                    last_session_id=last_sid,
                    source=src_name,
                    source_file_key=source_file_key,
                    record_iter=record_iter,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ingest_all: parse for %s/%s failed: %s",
                    src_name,
                    source_file_key,
                    exc,
                )
                reports.append(IngestReport(source_file_key, 0, 0, 1, 0, 0))
                continue

            reports.append(
                _dedup_and_commit_parsed(
                    conn,
                    parsed,
                    src_name=src_name,
                    source_file_key=source_file_key,
                    msg_seen=msg_seen,
                    ext_seen=ext_seen,
                    msg_cwd_idx=msg_cwd_idx,
                    msg_ts_idx=msg_ts_idx,
                    msg_text_idx=msg_text_idx,
                    ext_cwd_idx=ext_cwd_idx,
                    ext_ts_idx=ext_ts_idx,
                    ext_text_idx=ext_text_idx,
                    update_profiles=update_profiles,
                )
            )

    return reports


def _available_sources(names: list[str] | None) -> list[Any]:
    """Resolve the active SessionSource list, defensively.

    * ``names is None`` → every registered source whose ``is_available()``
      returns truthy. ``is_available()`` calls that *raise* are logged and
      the source is skipped (so a buggy adapter can't break ingest of the
      other adapters).
    * ``names`` non-empty → only those, in order; missing names are logged
      and skipped.

    Returns an empty list if :mod:`lib.sources` itself is unimportable
    (caller falls back to the legacy Claude-Code-only path).
    """
    try:
        from lib.sources import all_sources, source_by_name  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        log.debug("ingest_all: lib.sources unavailable: %s", exc)
        return []

    if names is not None:
        out: list[Any] = []
        for n in names:
            s = source_by_name(n)
            if s is None:
                log.warning("ingest_all: unknown source %r — skipping", n)
                continue
            out.append(s)
        return out

    out = []
    for s in all_sources():
        try:
            avail = s.is_available()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ingest_all: %s.is_available() raised %s — skipping",
                getattr(s, "name", type(s).__name__),
                exc,
            )
            continue
        if avail:
            out.append(s)
    return out


def ingest_all(
    conn: sqlite3.Connection | None = None,
    projects_root: Path | None = _DEFAULT_PROJECTS_ROOT,
    cwd_filter: str | None = None,
    dry_run: bool = False,
    force_full: bool = False,
    db_path: Path | None = None,
    trigger: str = "manual",
    jobs: int = 1,
    sources: list[str] | None = None,
    bulk_load: bool = False,
) -> list[IngestReport]:
    """Walk every available source's sessions and ingest them.

    Connection management:
        * If ``conn`` is provided, we use it and never close it (caller owns).
        * Otherwise we open one from ``db_path`` (or :data:`DEFAULT_DB_PATH`)
          and close it before returning. This is the path the CLI uses.

    ``cwd_filter``: when set, restricts ingest to sessions whose cwd
    matches. For Claude Code, the historical slug encoding (``/`` → ``-``)
    is also accepted.
    ``dry_run``: walk and report but don't write — currently implemented as
    "open a fresh in-memory DB and ingest into that" so we still exercise the
    full pipeline. ``force_full``: re-scan every file from offset 0.

    ``sources``: optional list of source names to ingest from
    (``["claude_code"]`` reproduces the legacy single-source behavior).
    When ``None``, every registered source whose ``is_available()``
    returns truthy is ingested. When :mod:`lib.sources` is unavailable
    (bare branch without XW1), we silently fall back to the original
    ``<slug>/*.jsonl`` walker so this PR remains independently mergeable.

    ``bulk_load``: rebuild/oneshot mode — apply write-throughput PRAGMAs,
    drop FTS live-sync triggers during insert (rebuild FTS once at end),
    and skip per-file incremental profiles (cold consolidation does that).

    The XW8 cross-source dedup heuristic
    (:func:`index.multi_source.filter_dedup_rows`) is applied across
    sources when there is more than one active source. It is a *hint* —
    duplicates that slip past it land in the DB tagged with their own
    ``source`` value, which downstream queries can still filter on.
    """
    # Connection setup.
    owns_conn = False
    if conn is None:
        # Lazy import to avoid an import cycle at module load.
        from index.db import DEFAULT_DB_PATH, connect

        if dry_run:
            # Throwaway in-memory DB so the CLI's --dry-run mode actually
            # exercises the pipeline without touching the real index.
            conn = connect(Path(":memory:"))
        else:
            target = Path(db_path).expanduser() if db_path else Path(DEFAULT_DB_PATH)
            conn = connect(target)
        owns_conn = True

    if projects_root is None:
        projects_root = _DEFAULT_PROJECTS_ROOT
    projects_root = Path(projects_root).expanduser()

    reports: list[IngestReport] = []
    run_t0 = time.monotonic()
    update_profiles = not bulk_load
    bulk_active = False

    try:
        if bulk_load and not dry_run:
            from index.db import (
                apply_bulk_load_pragmas,
                drop_fts_sync_triggers,
            )

            apply_bulk_load_pragmas(conn)
            drop_fts_sync_triggers(conn)
            bulk_active = True
            log.info(
                "ingest_all: bulk_load on "
                "(sync=OFF, large cache, FTS triggers deferred, profiles deferred)"
            )

        # ----- Multi-source path -------------------------------------------
        # Activate when the caller asked for specific sources OR when
        # lib.sources is importable AND has more than just claude_code
        # available. Falling back to the legacy single-source walker when
        # only claude_code is active preserves the original behavior
        # (including the parallel-jobs fast path) byte-for-byte.
        active = _available_sources(sources)
        # Honor `projects_root` override for the claude_code source — without
        # this, tests passing a synthetic projects_root would silently scan
        # the real ~/.claude/projects/. Other adapters resolve their own paths
        # from env vars / well-known dirs, so they're unaffected.
        root_overridden = bool(projects_root) and str(projects_root) != str(_DEFAULT_PROJECTS_ROOT)
        if root_overridden:
            for s in active:
                if getattr(s, "name", None) == "claude_code":
                    s.projects_root = projects_root  # type: ignore[attr-defined]
            # Hermetic override: do NOT also pull real OpenCode/Grok/Codex
            # trees unless the caller named sources= explicitly. Otherwise
            # empty synthetic roots (tests, CLI --projects-root) still fan
            # out into live session data and hang on operator-profile work.
            if sources is None:
                active = [s for s in active if getattr(s, "name", None) == "claude_code"]
        non_cc_active = any(getattr(s, "name", None) != "claude_code" for s in active)
        explicit = sources is not None
        # When the caller passed an explicit ``sources=`` list, we are
        # ALWAYS on the multi-source path even if the resolved list is
        # empty — otherwise ``sources=['unknown']`` would silently fall
        # through to the legacy ~/.claude/projects walker, which is
        # surprising and breaks tests.
        use_multi_source = explicit or (bool(active) and non_cc_active)

        if use_multi_source:
            reports.extend(
                _ingest_all_multi_source(
                    conn=conn,
                    active=active,
                    cwd_filter=cwd_filter,
                    force_full=force_full,
                    jobs=jobs,
                    update_profiles=update_profiles,
                )
            )
        else:
            # Legacy Claude-Code-only path (preserved verbatim).
            if not projects_root.exists():
                _record_ingest_run(conn, reports, run_t0, trigger)
                return reports

            files = _discover_jsonl_files(projects_root, cwd_filter)

            # Parallel path is only worth spinning up the pool for if we have
            # multiple files AND the caller asked for >1 worker AND we're not
            # in dry-run mode (which uses an in-memory DB workers can't see).
            use_parallel = jobs > 1 and len(files) > 1 and not dry_run

            if not use_parallel:
                for path in files:
                    try:
                        start_offset, rotated, last_sid = _resolve_cursor(
                            conn, path, force_full=force_full
                        )
                        parsed = _parse_file_pure(
                            path,
                            start_offset=start_offset,
                            rotated=rotated,
                            last_session_id=last_sid,
                        )
                        reports.append(
                            _commit_parsed(
                                conn, parsed, update_profiles=update_profiles
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("ingest_all: %s failed: %s", path, exc)
                        reports.append(
                            IngestReport(str(path), 0, 0, 1, 0, 0),
                        )
            else:
                # Resolve cursors up-front (workers don't touch the DB).
                # Then fan parsing out to a process pool and funnel results
                # back to the single-writer commit path. as_completed()
                # lets the main process start writing the moment the first
                # worker finishes, overlapping CPU-bound parsing with
                # serialized DB writes.
                import concurrent.futures as cf

                worker_args: list[tuple] = []
                for path in files:
                    start_offset, rotated, last_sid = _resolve_cursor(
                        conn, path, force_full=force_full
                    )
                    worker_args.append((str(path), start_offset, rotated, last_sid, "claude_code"))

                log.info(
                    "ingest_all: parallel parse, files=%d jobs=%d",
                    len(files),
                    jobs,
                )
                with cf.ProcessPoolExecutor(max_workers=jobs) as pool:
                    futures = {pool.submit(_parse_worker, args): args[0] for args in worker_args}
                    for fut in cf.as_completed(futures):
                        src = futures[fut]
                        try:
                            parsed = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "ingest_all: worker for %s failed: %s",
                                src,
                                exc,
                            )
                            reports.append(IngestReport(src, 0, 0, 1, 0, 0))
                            continue
                        try:
                            reports.append(
                                _commit_parsed(
                                    conn, parsed, update_profiles=update_profiles
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "ingest_all: commit for %s failed: %s",
                                src,
                                exc,
                            )
                            reports.append(IngestReport(src, 0, 0, 1, 0, 0))

        # Record the ingest-run summary BEFORE the maintenance commands so the
        # row reflects the actual data work. Defensive: skips silently on a
        # pre-MA1 schema.
        _record_ingest_run(conn, reports, run_t0, trigger)

        if bulk_active:
            from index.db import (
                rebuild_fts_indexes,
                recreate_fts_sync_triggers,
                restore_default_pragmas,
            )

            log.info("ingest_all: bulk_load finishing — rebuild FTS + restore PRAGMAs")
            rebuild_fts_indexes(conn)
            recreate_fts_sync_triggers(conn)
            restore_default_pragmas(conn)
            bulk_active = False

        # Cheap maintenance after a bulk ingest helps the planner pick the
        # right indexes for FTS-joined cwd/kind/ts queries.
        with contextlib.suppress(sqlite3.DatabaseError):
            conn.execute("ANALYZE")

        # WAL files grow without bound between checkpoints; TRUNCATE forces
        # the WAL back to zero bytes once readers have caught up. Skip
        # silently on errors (e.g. read-only DB, no-WAL journal mode).
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None:
                # PRAGMA returns (busy, log_pages, checkpointed_pages).
                try:
                    busy, log_pages, ckpt_pages = row[0], row[1], row[2]
                    log.info(
                        "ingest_all: wal_checkpoint(TRUNCATE) busy=%s log_pages=%s checkpointed=%s",
                        busy,
                        log_pages,
                        ckpt_pages,
                    )
                except (IndexError, TypeError):
                    log.debug("ingest_all: wal_checkpoint returned %r", row)
        except sqlite3.DatabaseError as exc:
            log.debug("ingest_all: wal_checkpoint failed: %s", exc)
    finally:
        if bulk_active and conn is not None:
            # Crash mid-bulk: still restore triggers/PRAGMAs so the DB is
            # usable for incremental hooks even if FTS is incomplete.
            with contextlib.suppress(Exception):
                from index.db import (
                    rebuild_fts_indexes,
                    recreate_fts_sync_triggers,
                    restore_default_pragmas,
                )

                rebuild_fts_indexes(conn)
                recreate_fts_sync_triggers(conn)
                restore_default_pragmas(conn)
        if owns_conn and conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    return reports


# Keep `os` referenced even when not used (linters complain about unused
# imports otherwise; in practice `os` is used by future expansion of this
# module — e.g. honoring ``TOTAL_RECALL_DISABLE_INGEST``).
_ = os
