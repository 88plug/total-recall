"""Cross-source orchestration + dedup helpers for the multi-CLI index.

XW8 added the ability to ingest from every adapter under
:mod:`lib.sources` (Codex, OpenCode, Gemini CLI, Continue, Cline, Cursor,
Aider, ...), not just Claude Code. With multiple sources comes the rare
but real possibility of the *same* conversation showing up twice — e.g.
the user runs OpenCode and Continue side by side, both pointed at the
same project, both capturing transcripts of the same chat session. The
heuristic in this module deduplicates such overlaps at insert time so
the recall index doesn't return two copies of every result.

Design choices:

* **Source priority** is fixed (``claude_code > codex > opencode >
  gemini_cli > continue > cline > cursor > aider``). The list is short
  and the ordering reflects which adapter retains the most metadata
  (Claude Code's JSONL is the richest — usage blocks, sidechains, plan
  mode, etc.).
* **Dedup key** is ``(cwd, minute_bucket(ts), sha256(text[:200]))``.
  Same cwd + same minute + same content prefix → the message is treated
  as the same physical conversation event. The 200-char prefix tolerates
  small whitespace / trailing-newline differences without false
  positives on short messages (which are usually distinctive anyway).
* **Best-effort, not transactional**. The dedup is a *hint*: if we miss
  a duplicate the index stays correct (just slightly noisy). If we
  over-suppress a duplicate we lose at most one row of redundant data.
  The trade-off favors recall quality over completeness.
* **No DB side effects** in :func:`dedup_key` / :func:`source_priority`
  / :func:`should_suppress` — they are pure functions so the test suite
  can exercise them without a connection.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable

log = logging.getLogger(__name__)

__all__ = [
    "SOURCE_PRIORITY",
    "source_priority",
    "minute_bucket",
    "text_hash",
    "dedup_key",
    "should_suppress",
    "filter_dedup_rows",
]


# Lower index = higher priority. Sources not listed get priority == len(list)
# so they sort *last* (never preferred over a listed source).
SOURCE_PRIORITY: tuple[str, ...] = (
    "claude_code",
    "codex",
    "opencode",
    "gemini_cli",
    "continue",
    "cline",
    "cursor",
    "aider",
)


def source_priority(name: str | None) -> int:
    """Return the priority rank of ``name`` (lower = preferred)."""
    if not name:
        return len(SOURCE_PRIORITY) + 1
    try:
        return SOURCE_PRIORITY.index(name)
    except ValueError:
        return len(SOURCE_PRIORITY)


def minute_bucket(ts: int | float | None) -> int | None:
    """Bucket a unix-second timestamp to the minute (``ts // 60``).

    Returns ``None`` if ``ts`` is missing / not numeric — callers should
    treat that as "skip dedup, can't compare".
    """
    if ts is None:
        return None
    try:
        return int(ts) // 60
    except (TypeError, ValueError):
        return None


def text_hash(text: str | None) -> str | None:
    """SHA256 of the first 200 chars of ``text`` (None on empty input).

    200 chars is enough to be content-distinctive without being so long
    that whitespace / EOL differences kill the match. Hex-encoded for
    cheap equality in a Python set.
    """
    if not text:
        return None
    prefix = text[:200]
    if not prefix.strip():
        return None
    return hashlib.sha256(prefix.encode("utf-8", "replace")).hexdigest()


def dedup_key(
    cwd: str | None, ts: int | float | None, text: str | None
) -> tuple[str, int, str] | None:
    """Build a cross-source dedup key, or return ``None`` if unbuildable.

    ``None`` means at least one of the three components is missing —
    callers must treat that row as un-dedupable and insert it as-is.
    """
    if cwd is None:
        return None
    bucket = minute_bucket(ts)
    if bucket is None:
        return None
    h = text_hash(text)
    if h is None:
        return None
    return (cwd, bucket, h)


def should_suppress(candidate_source: str, existing_source: str) -> bool:
    """Return True iff ``candidate`` should be skipped because
    ``existing`` is the higher-priority source for the same dedup key.

    Equal priorities → keep both (no suppression). Lower number wins.
    """
    return source_priority(candidate_source) > source_priority(existing_source)


def filter_dedup_rows(
    rows: Iterable[tuple],
    *,
    source: str,
    cwd_idx: int,
    ts_idx: int,
    text_idx: int,
    seen: dict[tuple[str, int, str], str] | None = None,
) -> tuple[list[tuple], dict[tuple[str, int, str], str], int]:
    """Filter ``rows`` against an in-memory dedup table.

    ``rows`` is an iterable of row tuples (e.g. what
    :func:`index.ingest._row_for_message` produces). ``cwd_idx``,
    ``ts_idx``, ``text_idx`` are the positions of the cwd / ts / text
    columns in the tuple — kept parameteric so this helper works for
    both ``messages`` rows and ``extractions`` rows without forcing a
    common shape.

    ``seen`` is the running dedup map (key → source name of the row
    already accepted). Callers thread it across multiple sources within
    one ingest pass; pass ``None`` for the first call and reuse the
    returned dict for subsequent calls.

    Returns ``(kept_rows, updated_seen, suppressed_count)``.

    Behavior:
        * Row with un-buildable key → kept (no dedup possible).
        * Key not in ``seen`` → kept, recorded as owned by ``source``.
        * Key in ``seen`` and the existing source has equal-or-better
          priority → row suppressed.
        * Key in ``seen`` but ``source`` has BETTER priority → the new
          row replaces the prior one in ``seen``; the older row stays
          in whatever batch it was already added to (we don't reach back
          to remove it — this is a best-effort heuristic, not a
          consistency guarantee). The replacement still ends up in the
          DB which is the correct behavior: prefer the richer source.
    """
    if seen is None:
        seen = {}
    kept: list[tuple] = []
    suppressed = 0
    for row in rows:
        try:
            cwd = row[cwd_idx]
            ts = row[ts_idx]
            text = row[text_idx]
        except (IndexError, TypeError):
            kept.append(row)
            continue
        key = dedup_key(cwd, ts, text)
        if key is None:
            kept.append(row)
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = source
            kept.append(row)
            continue
        if existing == source:
            # Same source duplicate — pass through; the table-level UNIQUE
            # (message_uuid) handles intra-source dedup separately.
            kept.append(row)
            continue
        if should_suppress(source, existing):
            suppressed += 1
            log.debug(
                "filter_dedup_rows: suppressing %s row at cwd=%s "
                "(superseded by %s)", source, cwd, existing,
            )
            continue
        # New source is preferred — update ownership but still keep the
        # row. The previously-inserted row will stay in the DB but with
        # `dedup_superseded_by_source` set by the caller if it cares.
        seen[key] = source
        kept.append(row)
    return kept, seen, suppressed
