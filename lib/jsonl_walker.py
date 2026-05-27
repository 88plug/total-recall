"""Streaming line-by-line walker for session-transcript ``.jsonl`` files.

Design goals:

* **Never slurp.** Files reach hundreds of MB; iterate with ``for line in f``.
* **Byte-offset tracking.** Yield the *next* byte offset after each line so
  callers can checkpoint and resume — useful for incremental indexers tailing
  an open session.
* **Tolerant.** Blank lines are skipped. A truncated final line (no trailing
  newline, JSON cut mid-object) is *not* a fatal error; we drop it on the
  floor since the harness will rewrite it on the next append.
* **Encoding-safe.** UTF-8 with ``errors="replace"``; transcripts occasionally
  contain odd bytes from tool output.

This module is read-only against the corpus.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional

from lib.schema import Record, parse_record

DEFAULT_PROJECTS_ROOT = Path("~/.claude/projects").expanduser()


# ---------------------------------------------------------------------------
# slug ↔ cwd
# ---------------------------------------------------------------------------


def derive_cwd_from_slug(slug: str) -> str:
    """Convert a directory slug back to its cwd path.

    The Claude Code harness names project directories by taking ``cwd`` and
    replacing every ``/`` with ``-``. A leading slash becomes a leading
    dash. We round-trip by replacing dashes with slashes — preserving the
    leading slash that comes from the leading dash.

    Examples:
        ``-home-operator-amnesia``        → ``/home/operator/amnesia``
        ``-home-operator``                → ``/home/operator``
        ``-home-operator-ip-service-for-docker`` → ``/home/operator/ip-service-for-docker``

    Caveat: original dashes in directory names are *lossy* — there is no way
    to distinguish them from path separators. Callers that need ground-truth
    ``cwd`` should read it from a non-null record body instead (line 1 has
    ``cwd:null``; line 3+ usually has the real value).
    """

    if not slug:
        return slug
    return slug.replace("-", "/")


def derive_slug_from_cwd(cwd: str) -> str:
    """Inverse of :func:`derive_cwd_from_slug`.

    Lossy for directory names containing dashes — see that function's docstring.
    """

    return cwd.replace("/", "-")


# ---------------------------------------------------------------------------
# session-file discovery
# ---------------------------------------------------------------------------


def _session_file_filter(p: Path) -> bool:
    """Top-level session ``.jsonl`` files only (UUID-named, no nesting)."""
    return p.is_file() and p.suffix == ".jsonl" and len(p.stem) >= 32


def session_files_for_cwd(projects_root: Path, cwd: str) -> list[Path]:
    """Session files for a given ``cwd``.

    Returns an empty list if the project directory does not exist.
    """

    slug = derive_slug_from_cwd(cwd)
    proj_dir = projects_root / slug
    if not proj_dir.is_dir():
        return []
    out = [p for p in proj_dir.iterdir() if _session_file_filter(p)]
    out.sort()
    return out


def all_sessions(
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(cwd, session_jsonl_path)`` for every session under
    ``projects_root``.

    ``cwd`` is derived from the slug (see caveat in
    :func:`derive_cwd_from_slug`). Order is project-then-filename, both
    sorted lexicographically — stable for testability.
    """

    if not projects_root.is_dir():
        return
    for proj in sorted(projects_root.iterdir()):
        if not proj.is_dir():
            continue
        cwd = derive_cwd_from_slug(proj.name)
        for s in sorted(proj.iterdir()):
            if _session_file_filter(s):
                yield cwd, s


# ---------------------------------------------------------------------------
# the walker
# ---------------------------------------------------------------------------


def iter_records(
    path: Path, start_offset: int = 0
) -> Iterator[tuple[int, Record]]:
    """Yield ``(next_byte_offset, Record)`` for each well-formed line in ``path``.

    Behavior:

    * ``start_offset`` ≥ 0 seeks before reading. ``0`` (default) reads from
      the top. A non-zero offset must correspond to a line boundary
      (typically a previous yield's ``next_byte_offset``).
    * Blank lines are skipped silently.
    * A line that fails ``json.loads`` is skipped silently. In practice this
      is the truncated-tail case: the harness is mid-append and the final
      record is not yet whole. A subsequent call with ``start_offset`` set
      to that line's *start* will replay it once it's complete.
    * The yielded byte offset is the position *after* the consumed line
      (including its newline). Save it as your next-call ``start_offset``.

    Raises ``FileNotFoundError`` if the path does not exist.
    """

    path = Path(path)
    with path.open("rb") as f:
        if start_offset:
            f.seek(start_offset)
        # We need byte-accurate offsets, so track them ourselves rather than
        # trusting f.tell() inside iteration.
        offset = start_offset
        while True:
            line = f.readline()
            if not line:
                break  # EOF
            line_len = len(line)
            offset += line_len
            stripped = line.strip()
            if not stripped:
                continue
            if not line.endswith(b"\n"):
                # Truncated final line — drop it; the harness will rewrite.
                continue
            try:
                obj = json.loads(stripped.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            # byte_offset = start of this line (offset - line_len)
            rec = parse_record(obj, byte_offset=offset - line_len)
            yield offset, rec


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------


def read_all(path: Path) -> list[Record]:
    """Eager helper — collect every record in ``path``.

    Convenient for tests on small files; do **not** use on multi-hundred-MB
    sessions in production paths. Always prefer the streaming
    :func:`iter_records` interface.
    """

    return [r for _, r in iter_records(Path(path))]


def file_size(path: Path) -> int:
    """Current size in bytes; useful for callers checkpointing tail reads."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def derive_session_id_from_path(path: Path) -> Optional[str]:
    """Pull the session UUID out of the filename (``<uuid>.jsonl``)."""
    p = Path(path)
    stem = p.stem
    # session UUIDs are 36 chars; defensively allow some slack
    if 32 <= len(stem) <= 64:
        return stem
    return None
