"""Multi-source record collector.

Iterates every registered :class:`~lib.sources.base.SessionSource` adapter,
discovers its sessions, and yields the normalized :class:`lib.schema.Record`
objects from all of them in a single stream.

Designed for consolidation / cold-rebuild passes that need to mine ALL CLI
sources (Claude Code, OpenCode, Codex, Gemini CLI, …) rather than the
traditional ``~/.claude/projects``-only glob.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from lib.sources import all_sources
from lib.schema import Record

log = logging.getLogger(__name__)


def iter_all_source_records() -> Iterator[Record]:
    """Yield :class:`lib.schema.Record` objects from every available source.

    Adapter failures are caught and logged as warnings so a single broken
    source never aborts the whole consolidation pass.  Sources that report
    ``is_available() == False`` are skipped cheaply without opening files.
    """
    for src in all_sources():
        if not src.is_available():
            continue
        src_name = getattr(src, "name", repr(src))
        try:
            for session in src.discover_sessions():
                try:
                    for _cursor, rec in src.iter_records(session):
                        yield rec
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "source %s failed reading session %s: %s",
                        src_name,
                        getattr(session, "session_id", "?"),
                        exc,
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("source %s failed during consolidation: %s", src_name, exc)


def materialize_all_source_records() -> list[Any]:
    """Return all source records as a list.

    Use when the caller needs to iterate the record stream more than once
    (e.g. building a uuid-keyed DAG dict for satisfaction extraction).
    Memory cost scales with corpus size; prefer :func:`iter_all_source_records`
    for single-pass use cases.
    """
    return list(iter_all_source_records())
