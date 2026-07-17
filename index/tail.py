"""Polling tail loop for the total-recall index.

Re-runs :func:`index.ingest.ingest_all` on a fixed interval (default 30s).
Each iteration walks ``~/.claude/projects`` and re-ingests any file whose
``(inode, size, mtime)`` differs from the last ``ingest_state`` row. The
per-file logic is in :mod:`index.ingest`; this module just sets the cadence
and handles signals.

We use polling rather than inotify because:

* ``inotify`` adds a Linux-only dependency and doesn't survive across
  network filesystems (some users keep ``~/.claude`` on NFS / sync mounts).
* 30s is well under the human latency tolerance for "I came back from a
  Claude Code session 5 min ago, what did we decide?".
* The DB updates are idempotent and cheap when nothing changed (a stat()
  per file).

Inotify is an OK upgrade later; the public API of this module won't change.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from index.ingest import _DEFAULT_PROJECTS_ROOT, ingest_all

log = logging.getLogger(__name__)

__all__ = ["tail_loop"]


def _install_signal_handlers(stop: Callable[[], None]) -> None:
    """Wire SIGINT/SIGTERM to the caller-supplied stop function.

    Wrapped in try/except because :func:`signal.signal` raises ``ValueError``
    when called off the main thread — e.g. when the MCP server runs the
    tailer in a worker.
    """

    def _handler(signum: int, frame: object | None) -> None:  # noqa: ARG001
        log.info("tail_loop: received signal %s, stopping", signum)
        stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Off-main-thread or unsupported on this OS; that's fine.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handler)


def tail_loop(
    conn: sqlite3.Connection,
    interval: int = 30,
    projects_root: Path = _DEFAULT_PROJECTS_ROOT,
    max_iterations: int | None = None,
) -> None:
    """Run :func:`ingest_all` every ``interval`` seconds until interrupted.

    ``max_iterations`` is escape-hatch for tests — pass an int to bound the
    loop. Production callers leave it as ``None`` for indefinite tailing.
    """
    stopped = {"v": False}

    def _stop() -> None:
        stopped["v"] = True

    _install_signal_handlers(_stop)

    iterations = 0
    log.info(
        "tail_loop: polling %s every %ss",
        projects_root,
        interval,
    )
    while not stopped["v"]:
        t0 = time.monotonic()
        try:
            reports = ingest_all(conn, projects_root=projects_root)
        except Exception as exc:  # noqa: BLE001
            log.warning("tail_loop: ingest_all raised: %s", exc)
            reports = []

        new_msgs = sum(r.new_messages for r in reports)
        new_exts = sum(r.new_extractions for r in reports)
        if new_msgs or new_exts:
            log.info(
                "tail_loop: +%d messages, +%d extractions across %d files in %dms",
                new_msgs,
                new_exts,
                len(reports),
                int((time.monotonic() - t0) * 1000),
            )

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break

        # Sleep in small chunks so a SIGINT lands within ~0.25s.
        slept = 0.0
        while slept < interval and not stopped["v"]:
            chunk = min(0.25, interval - slept)
            time.sleep(chunk)
            slept += chunk
