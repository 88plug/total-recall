"""Abstract base for every CLI client's session-storage adapter.

The downstream pipeline (ingest → extractors → index → recall) is wired
against :class:`SessionSource`. As long as an adapter yields canonical
``lib.schema.Record`` instances from :meth:`SessionSource.iter_records`,
the rest of the system does not care whether the underlying storage is
JSONL files (Claude Code, Codex, Aider), a SQLite DB (OpenCode, Cursor),
or some other shape (Continue, Cline, Gemini CLI).

The :class:`SessionFile` dataclass is the lightweight handle returned by
:meth:`SessionSource.discover_sessions`. It carries just enough metadata
for the ingest loop to do checkpointing (``last_modified``) and for the
``total-recall sources list`` CLI to surface sessions to the user
(``session_id``, ``cwd``) without opening every transcript.

Adapter modules register themselves at import time by appending their
class to :data:`SOURCES`. Concrete adapters live in sibling modules
(``lib.sources.claude_code``, ``lib.sources.opencode``, ...) and are
explicitly imported from :mod:`lib.sources` to trigger registration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionFile:
    """A locatable session transcript from a specific CLI client.

    ``path`` is the canonical handle — a JSONL file for most clients, a
    SQLite DB path for OpenCode/Cursor. Adapters use whatever they need
    from :attr:`extra` (table name, project hash, etc.) to actually read
    records.

    ``started_at`` is best-effort; many sources can only learn it by
    reading the first record, which the lazy discovery path deliberately
    avoids. Treat ``None`` as "not known yet, will be filled in once the
    walker reaches that session".
    """

    source: str
    """Adapter name: ``"claude_code" | "opencode" | "gemini_cli" | "codex"
    | "cursor" | "continue" | "cline" | "aider"``."""

    path: Path
    """Canonical handle — file or DB path."""

    cwd: str | None
    """Working directory this session ran in (``None`` if unknown)."""

    session_id: str
    """Source-specific session id (UUID, ULID, file stem, row id, ...)."""

    started_at: float | None
    """Unix seconds at which the session started; ``None`` if unknown."""

    last_modified: float
    """Unix seconds; ``mtime`` of the underlying file. Used for incremental
    ingestion: skip if ``<=`` the last-indexed timestamp."""

    extra: dict = field(default_factory=dict)
    """Source-specific metadata. Free-form. Examples: ``{"slug": ...}``
    for Claude Code, ``{"table": ..., "row_id": ...}`` for SQLite-backed
    sources."""


class SessionSource(ABC):
    """One CLI client's session storage.

    Adapters produce :class:`lib.schema.Record` instances regardless of
    the underlying format (JSONL, SQLite, markdown). Downstream code
    (extractors, index, recall, etc.) treats every Record identically.

    Concrete subclasses must set the class-level :attr:`name` attribute
    and implement the three abstract methods below. They should also
    register themselves with the package by appending their class to
    :data:`SOURCES` (see :mod:`lib.sources.claude_code` for the pattern).
    """

    name: str
    """Stable identifier — used by :func:`source_by_name` and surfaced
    in :attr:`SessionFile.source`. Must match the class-level constant."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` if this source has detectable on-disk sessions.

        Used by ``total-recall sources list`` to skip un-installed
        clients. Implementations should be cheap (``Path.exists()``
        level), never read file bodies.
        """

    @abstractmethod
    def discover_sessions(self) -> Iterator[SessionFile]:
        """Yield every session this source can find.

        Lazy: must not load record bodies. Used by ingest to enumerate
        work. Order is implementation-defined but should be stable
        (sorted by path or session_id) so callers can checkpoint.
        """

    @abstractmethod
    def iter_records(
        self, session: SessionFile, start_offset: int = 0
    ) -> Iterator[tuple[int, Any]]:
        """Yield ``(next_byte_offset_or_cursor, Record)`` for ``session``.

        Returns the same Record type from :mod:`lib.schema` regardless of
        source. Adapters do whatever schema translation is needed.

        For byte-stream sources (JSONL), the first element is a byte
        offset suitable for checkpointing. For row-cursor sources
        (SQLite), it is the row id of the just-yielded record. Either
        way, passing it back as ``start_offset`` resumes from there.
        """


# Adapters register themselves here at import time. Order is priority:
# earlier adapters get listed first by ``all_sources()``.
SOURCES: list[type[SessionSource]] = []


def all_sources() -> list[SessionSource]:
    """Return instantiated source objects, in priority (registration) order.

    Adapter modules must already have been imported (importing
    :mod:`lib.sources` is enough for the bundled ones).
    """

    return [cls() for cls in SOURCES]


def source_by_name(name: str) -> SessionSource | None:
    """Return the instantiated adapter whose :attr:`name` matches.

    Returns ``None`` if no adapter is registered under ``name``. The
    class-level ``name`` attribute is consulted (no need to instantiate
    twice — but we do, to keep the registry uniform).
    """

    for cls in SOURCES:
        if cls.name == name:
            return cls()
    return None
