"""Aider session-source adapter — markdown chat-history files.

Aider (https://aider.chat) does not have an MCP, does not emit JSONL, and
does not run any hooks total-recall could plug into. Its on-disk record
is three append-only files at the root of every git repo Aider has
touched:

* ``.aider.chat.history.md`` — **the one we care about.** Markdown log of
  every chat turn, ever, in this repo. Multiple "sessions" coexist in one
  file, separated by ``# aider chat started at YYYY-MM-DD HH:MM:SS``
  headers.
* ``.aider.input.history`` — readline input history. Duplicates the user
  half of the chat log and adds nothing; skipped.
* ``.aider.llm.history`` — optional verbose LLM log, only present when
  the user ran with ``--llm-history-file``. Skipped.

**Information-poor warning.** Compared to JSONL/SQLite sources, an Aider
transcript has *no* per-message tokens, *no* model identifier per turn,
*no* tool-call structure, *no* request id. It is plain markdown:

* Lines beginning with ``####`` are user messages (one user turn may span
  many consecutive ``####`` lines).
* Lines beginning with ``> `` are assistant output (blockquote). Nested
  ``>>`` blocks are tool / lint / shell output.
* Anything else (SEARCH/REPLACE blocks, code fences) is content inside
  whichever turn it appears in — we attach it to the current speaker.

Text-driven extractors (corrections, decisions, bans, self-corrections,
voice profile) still fire normally on this content. Cost tracking, model
attribution, and structured tool-result mining will skip Aider sessions
silently — there is simply no signal to extract.

Discovery is the hard part. Aider doesn't centralize anything; we have
to walk the filesystem looking for repos with ``.aider.chat.history.md``.
We restrict the search to a small set of dev roots (``~``, ``~/projects``,
``~/src``, ``~/code``), prune obvious noise dirs, and cap at
:attr:`AiderSource.max_files` matches / :attr:`AiderSource.max_seconds`
wall time so the cheap ``is_available``/``discover_sessions`` contract
holds even on huge home dirs.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from lib.schema import (
    AssistantRecord,
    Block,
    Record,
    UserRecord,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource

# ---------------------------------------------------------------------------
# Markdown grammar
# ---------------------------------------------------------------------------

#: Session boundary. Aider writes this header every time the CLI starts a
#: new chat against this repo. Format is fixed.
SESSION_HEADER = re.compile(
    r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)

#: User line. ``#### `` (4 hashes + space) is the canonical prefix; a bare
#: ``####`` denotes a blank user line (common between paragraphs).
USER_LINE = re.compile(r"^####(?: ?(.*))?$")

#: Assistant line. Aider uses ``> `` blockquote; a bare ``>`` is a blank
#: blockquote line. Nested ``>>`` blocks (tool/lint output) are still
#: assistant content for our purposes — captured as part of the same turn.
ASSISTANT_LINE = re.compile(r"^>(?:\s?(.*))?$")

#: Dirs we never want to recurse into during discovery.
PRUNE_DIRS = frozenset(
    {
        ".git",  # contains massive .git/objects trees
        "node_modules",
        "target",
        "dist",
        "build",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".cache",
        ".gradle",
        ".idea",
        ".vscode",
        "vendor",
        "Pods",
        "DerivedData",
    }
)

HISTORY_FILENAME = ".aider.chat.history.md"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AiderSource(SessionSource):
    """Adapter for Aider's per-repo ``.aider.chat.history.md`` files.

    A single markdown file holds N sessions; we yield one
    :class:`SessionFile` per session, carrying byte offsets in
    :attr:`SessionFile.extra` (``start_offset`` / ``end_offset``) so
    :meth:`iter_records` can parse just that slice on demand.
    """

    name = "aider"

    #: Hard cap on discovered history files. Aider users typically have
    #: tens of these at most; if we hit four digits we are almost
    #: certainly recursing into something we shouldn't.
    max_files: int = 1000

    #: Hard cap on discovery wall time (seconds). Discovery is meant to
    #: be cheap; bail out cleanly rather than hang the CLI.
    max_seconds: float = 10.0

    def __init__(
        self,
        search_roots: Optional[list[Path]] = None,
        *,
        max_files: Optional[int] = None,
        max_seconds: Optional[float] = None,
    ) -> None:
        self.search_roots: list[Path] = (
            list(search_roots) if search_roots is not None else [Path.home()]
        )
        if max_files is not None:
            self.max_files = max_files
        if max_seconds is not None:
            self.max_seconds = max_seconds
        # Cached find results — lazily populated by _find_history_files.
        self._cache: Optional[list[Path]] = None

    # ---- availability ---------------------------------------------------

    def is_available(self) -> bool:
        """Cheap check: any obvious dev-root child has a history file?

        Deliberately shallow — we only inspect the immediate children of
        each ``<root>`` and ``<root>/{projects,src,code}``. A full walk
        is reserved for :meth:`discover_sessions`.
        """

        for root in self.search_roots:
            for parent in (root, root / "projects", root / "src", root / "code"):
                if not parent.exists():
                    continue
                try:
                    for child in parent.iterdir():
                        if not child.is_dir():
                            continue
                        if child.name in PRUNE_DIRS:
                            continue
                        if (child / HISTORY_FILENAME).is_file():
                            return True
                except (PermissionError, OSError):
                    continue
        return False

    # ---- discovery ------------------------------------------------------

    def _find_history_files(self) -> list[Path]:
        """Walk :attr:`search_roots`, return every ``.aider.chat.history.md``.

        Cached after the first call. Bounded by :attr:`max_files` and
        :attr:`max_seconds` — when either cap trips we return what we
        have rather than raise, so callers always get a usable list.
        """

        if self._cache is not None:
            return self._cache

        found: list[Path] = []
        seen_real: set[Path] = set()
        deadline = time.monotonic() + self.max_seconds

        for root in self.search_roots:
            if not root.exists():
                continue
            try:
                resolved = root.resolve()
            except OSError:
                resolved = root
            if resolved in seen_real:
                continue
            seen_real.add(resolved)

            for dirpath, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=False
            ):
                # Prune in-place so os.walk doesn't descend.
                dirnames[:] = [
                    d for d in dirnames
                    if d not in PRUNE_DIRS and not d.startswith(".aider")
                ]
                if HISTORY_FILENAME in filenames:
                    found.append(Path(dirpath) / HISTORY_FILENAME)
                    if len(found) >= self.max_files:
                        self._cache = found
                        return found
                if time.monotonic() > deadline:
                    self._cache = found
                    return found

        # Deterministic order — sort by path for stable downstream output.
        found.sort()
        self._cache = found
        return found

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield one :class:`SessionFile` per ``# aider chat started at``
        section across every discovered history file."""

        for md_file in self._find_history_files():
            try:
                stat = md_file.stat()
            except OSError:
                continue
            try:
                sessions = self._split_sessions(md_file)
            except (OSError, UnicodeDecodeError):
                continue
            for sess in sessions:
                ts: datetime = sess["ts"]
                yield SessionFile(
                    source=self.name,
                    path=md_file,
                    cwd=str(md_file.parent),
                    session_id=f"{md_file.parent.name}:{ts.isoformat()}",
                    started_at=ts.timestamp(),
                    last_modified=stat.st_mtime,
                    extra={
                        "start_offset": sess["start"],
                        "end_offset": sess["end"],
                    },
                )

    @staticmethod
    def _split_sessions(md_file: Path) -> list[dict[str, Any]]:
        """Scan ``md_file`` for ``# aider chat started at`` headers and
        return one descriptor per session.

        Each descriptor has ``ts`` (UTC-naive :class:`datetime`),
        ``start`` (byte offset of the header line), and ``end`` (byte
        offset of the next header — exclusive — or the file size for the
        final session).

        File content **before** the first header is dropped: Aider always
        writes a header before any turn content, so anything earlier is
        either empty or a corrupted half-write.
        """

        with md_file.open("rb") as f:
            data = f.read()

        sessions: list[dict[str, Any]] = []
        offset = 0
        for raw_line in data.splitlines(keepends=True):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                line = raw_line.decode("utf-8", errors="replace")
            stripped = line.rstrip("\r\n")
            m = SESSION_HEADER.match(stripped)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    offset += len(raw_line)
                    continue
                if sessions:
                    sessions[-1]["end"] = offset
                sessions.append({"ts": ts, "start": offset, "end": len(data)})
            offset += len(raw_line)

        # Fill the trailing session's end with EOF.
        if sessions:
            sessions[-1]["end"] = len(data)
        return sessions

    # ---- record streaming ----------------------------------------------

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Record]]:
        """Parse one session's slice of the markdown.

        Yields ``(records_emitted, Record)`` pairs. Consecutive lines of
        the same role are grouped into a single :class:`UserRecord` or
        :class:`AssistantRecord` — the markdown grammar inherently emits
        a turn as a run of like-prefixed lines.

        ``start_offset`` is the count of records to skip (not a byte
        offset). The markdown is small enough per session that resuming
        mid-file is not interesting; we expose the parameter for
        SessionSource ABC compatibility.
        """

        extra = session.extra or {}
        start: int = int(extra.get("start_offset", 0))
        end: int = int(extra.get("end_offset", 0)) or None  # type: ignore[assignment]

        try:
            with session.path.open("rb") as f:
                f.seek(start)
                blob = f.read() if end is None else f.read(end - start)
        except OSError:
            return

        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            text = blob.decode("utf-8", errors="replace")

        ts: Optional[datetime] = None
        if session.started_at is not None:
            try:
                ts = datetime.fromtimestamp(session.started_at)
            except (OverflowError, OSError, ValueError):
                ts = None

        emitted = 0
        current_role: Optional[str] = None
        buffer: list[str] = []

        def _flush() -> Optional[Record]:
            nonlocal current_role, buffer
            if current_role is None or not buffer:
                current_role = None
                buffer = []
                return None
            content = "\n".join(buffer).rstrip()
            role = current_role
            current_role = None
            buffer = []
            if not content:
                return None
            return _make_record(role, content, session, ts)

        for line in text.splitlines():
            if SESSION_HEADER.match(line):
                # First (or any) session-header inside our slice — skip;
                # the slice should already be scoped to one session, but
                # if the file was rewritten between discovery and read we
                # don't want to bleed into the next session.
                rec = _flush()
                if rec is not None:
                    emitted += 1
                    if emitted > start_offset:
                        yield emitted, rec
                continue

            mu = USER_LINE.match(line)
            ma = ASSISTANT_LINE.match(line)
            if mu is not None:
                if current_role != "user":
                    rec = _flush()
                    if rec is not None:
                        emitted += 1
                        if emitted > start_offset:
                            yield emitted, rec
                    current_role = "user"
                buffer.append(mu.group(1) or "")
            elif ma is not None:
                if current_role != "assistant":
                    rec = _flush()
                    if rec is not None:
                        emitted += 1
                        if emitted > start_offset:
                            yield emitted, rec
                    current_role = "assistant"
                buffer.append(ma.group(1) or "")
            else:
                # Free-form content (SEARCH/REPLACE blocks, code fences,
                # diff bodies). Attach to whichever turn we're in; if we
                # haven't seen any prefix yet, drop the line.
                if current_role is None:
                    continue
                buffer.append(line)

        rec = _flush()
        if rec is not None:
            emitted += 1
            if emitted > start_offset:
                yield emitted, rec


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def _base_kwargs(session: SessionFile, ts: Optional[datetime]) -> dict[str, Any]:
    """Shared :class:`Record` base fields for Aider turns.

    Aider doesn't surface per-turn uuids, parent links, git branch, or
    version — we leave all of those ``None``. ``cwd`` is the git repo
    root (the directory containing the markdown file).
    """

    return dict(
        type="?",
        uuid=None,
        parent_uuid=None,
        session_id=session.session_id,
        ts=ts,
        cwd=session.cwd,
        git_branch=None,
        version=None,
        is_sidechain=False,
        raw={},
        byte_offset=0,
    )


def _make_record(
    role: str,
    content: str,
    session: SessionFile,
    ts: Optional[datetime],
) -> Record:
    """Build a :class:`UserRecord` or :class:`AssistantRecord` for one
    grouped turn of Aider markdown."""

    base = _base_kwargs(session, ts)
    if role == "assistant":
        base["type"] = "assistant"
        return AssistantRecord(
            **base,
            model=None,
            content=[Block(type="text", text=content, raw={"aider": True})],
            usage=None,
            stop_reason=None,
            message_id=None,
            request_id=None,
        )

    base["type"] = "user"
    return UserRecord(
        **base,
        content_kind="string",
        text=content,
        tool_results=[],
        tool_use_result_payload=None,
        is_compact_summary=False,
        is_meta=False,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

SOURCES.append(AiderSource)

__all__ = ["AiderSource"]
