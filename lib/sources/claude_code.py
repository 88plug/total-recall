"""Claude Code adapter — wraps the existing :mod:`lib.jsonl_walker`.

This is the reference implementation of :class:`lib.sources.base.SessionSource`
and the only one that ships with the project at the time the ABC landed.
All other adapters (OpenCode, Codex, Cursor, Continue, Cline, Aider,
Gemini CLI) follow the same pattern: discover sessions on disk, yield
:class:`SessionFile` handles, and translate the source's native records
into :class:`lib.schema.Record` instances inside :meth:`iter_records`.

For Claude Code there is no translation step — the underlying transcript
is already produced by :mod:`lib.jsonl_walker`. We just provide a stable
public interface so downstream code stays source-agnostic.

The existing :mod:`lib.jsonl_walker` module is **not** modified; it
remains the source of truth for the streaming JSONL semantics
(blank-line tolerance, truncated-tail handling, byte-offset tracking).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional

from lib.jsonl_walker import (
    derive_cwd_from_slug,
    derive_slug_from_cwd,
    iter_records as _cc_iter_records,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource


class ClaudeCodeSource(SessionSource):
    """Adapter for ``~/.claude/projects/<slug>/<session-uuid>.jsonl``.

    The projects-root directory is overridable for tests; production code
    should construct with no arguments and accept the default
    ``~/.claude/projects``.
    """

    name = "claude_code"

    def __init__(self, projects_root: Optional[Path] = None) -> None:
        self.projects_root = (
            projects_root if projects_root is not None
            else Path.home() / ".claude" / "projects"
        )

    def is_available(self) -> bool:
        """``True`` iff the projects directory exists on disk.

        Cheap — single ``stat()`` call. No JSONL is opened.
        """

        return self.projects_root.is_dir()

    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield a :class:`SessionFile` for every ``.jsonl`` under the root.

        Lazy: no file body is read. Order is project-then-filename, both
        sorted lexicographically, so callers can checkpoint. Files whose
        ``stat()`` fails (e.g. broken symlink) are silently skipped.
        """

        if not self.is_available():
            return
        for proj_dir in sorted(self.projects_root.iterdir()):
            if not proj_dir.is_dir():
                continue
            cwd = derive_cwd_from_slug(proj_dir.name)
            for jsonl in sorted(proj_dir.glob("*.jsonl")):
                if not jsonl.is_file():
                    continue
                try:
                    stat = jsonl.stat()
                except OSError:
                    continue
                yield SessionFile(
                    source=self.name,
                    path=jsonl,
                    cwd=cwd,
                    session_id=jsonl.stem,
                    started_at=None,  # filled in lazily by the record walker
                    last_modified=stat.st_mtime,
                    extra={"slug": proj_dir.name},
                )

    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Stream ``(next_byte_offset, Record)`` pairs from ``session``.

        Thin delegation to :func:`lib.jsonl_walker.iter_records`. The
        yielded ``Record`` is one of the dataclasses in :mod:`lib.schema`;
        unknown ``type`` values fall through to the base ``Record`` rather
        than raising, so the adapter is forward-compatible with additive
        schema changes from Claude Code itself.
        """

        yield from _cc_iter_records(session.path, start_offset=start_offset)


# Register at import time — :func:`lib.sources.all_sources` walks SOURCES.
SOURCES.append(ClaudeCodeSource)


# Re-exported for downstream convenience; the canonical home is still
# :mod:`lib.jsonl_walker`.
__all__ = [
    "ClaudeCodeSource",
    "derive_cwd_from_slug",
    "derive_slug_from_cwd",
]
