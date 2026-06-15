"""Bans + failed-attempts extractor.

Detects operator declarations of two flavors:

1.  **Bans** — "never use X", "stop suggesting Y", "we don't use Z". These get
    stored in the ``bans`` table so the model can call
    :func:`mcp_server.extras.bans_tools.check_banned` *before* defaulting to a
    provider/tool/framework.

2.  **Failed attempts** — "we tried X but it broke", "switched from A to B
    because...", "MIGRATED AWAY FROM ...". Logged so the model doesn't
    re-suggest something the operator has already burned cycles on.

Both flavors emit :class:`extractors.base.Extraction` rows; the indexer at
:mod:`index.bans` is responsible for collapsing them into the dedup'd
``bans`` / ``failed_attempts`` tables (reassertion-counting lives there).

Patterns are deliberately narrow — false positives in ``check_banned`` would
make the tool useless. When a pattern fires, the captured token *plus* the
verbatim source sentence are emitted; downstream callers need both.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator

from extractors.base import (
    DagLike,
    Extraction,
    Extractor,
    RecordLike,
    get_assistant_text_blocks,
    get_user_string,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ban patterns
# ---------------------------------------------------------------------------

# `(?i)never (ever )?(recommend|use|mention) X` — absolute.
_ABSOLUTE_NEVER_RE = re.compile(
    r"(?i)\bnever\s+(?:ever\s+)?(?:recommend|use|mention)\s+([\w][\w\.\-]*)",
)

# `(?i)we (never|don't|won't) use X` — preference / standing rule.
_PREFERENCE_RE = re.compile(
    r"(?i)\bwe\s+(?:never|don'?t|won'?t)\s+use\s+([\w][\w\.\-]*)",
)

# `(?i)stop (suggesting|recommending|using) X` — context ban (live correction).
_STOP_RE = re.compile(
    r"(?i)\bstop\s+(?:suggesting|recommending|using)\s+([\w][\w\.\-]*)",
)

# Public-vs-internal distinction: `never name X publicly` / `never name X in docs/pricing/footer`.
# Captured *before* the broader absolute pattern so we can attach the context_clause.
_PUBLIC_NAME_RE = re.compile(
    r"(?i)\bnever\s+name\s+([\w][\w\.\-]*)\s+(publicly|in\s+(?:docs|pricing|footer))",
)

# `(?i)no X ever` — absolute, terse.
_NO_X_EVER_RE = re.compile(
    r"(?i)\bno\s+([\w][\w\.\-]*)\s+ever\b",
)


# ---------------------------------------------------------------------------
# Failed-attempts patterns
# ---------------------------------------------------------------------------

_TRIED_FAILED_RE = re.compile(
    r"(?i)\bwe\s+tried\s+([\w][\w\.\-]*)"
    r"[^\.\n]*?(?:didn'?t\s+work|broke|failed|abandoned)",
)

# `switched from A to B because/after ...` or `switched to B from A because ...`
_SWITCHED_FROM_RE = re.compile(
    r"(?i)\bswitched\s+from\s+([\w][\w\.\-]*)\s+to\s+([\w][\w\.\-]*)"
    r"[^\.\n]*?(?:because|after)\s+([^\.\n]{1,200})",
)
_SWITCHED_TO_RE = re.compile(
    r"(?i)\bswitched\s+to\s+([\w][\w\.\-]*)\s+from\s+([\w][\w\.\-]*)"
    r"[^\.\n]*?(?:because|after)\s+([^\.\n]{1,200})",
)

# Uppercase variant: operators write `MIGRATED AWAY FROM X` for emphasis.
_MIGRATED_AWAY_RE = re.compile(
    r"\bMIGRATED AWAY FROM\s+([\w][\w\.\-]*)",
)

# Co-occurrence: `X is broken / fails / crashed / leaks`
_BROKEN_CO_RE = re.compile(
    r"(?i)\b([\w][\w\.\-]{2,})\s+(?:is\s+)?(broken|fails|crashed|leaks)\b",
)


# Common-noun guard for the broad co-occurrence rule — these capitalize as
# captures but are obviously not products.
_BROKEN_STOPWORDS = frozenset(
    {
        "it",
        "this",
        "that",
        "everything",
        "nothing",
        "something",
        "all",
        "build",
        "test",
        "thing",
    }
)


_BASE_SCORE_BAN = 0.8
_BASE_SCORE_FAILED = 0.7


class Bans(Extractor):
    """Emit ``ban`` and ``failed_attempt`` extractions from user + assistant text."""

    name = "bans"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            # Both user-strings and assistant-text-blocks are legitimate
            # sources: the operator declares bans in user turns; the assistant
            # often paraphrases them back ("you said never to use X") which
            # would otherwise inflate reassertion counts — but reassertion
            # accounting is done by the indexer using UNIQUE keys, so it's
            # safe to harvest from both.
            blocks: list[str] = []
            u = get_user_string(rec)
            if u is not None:
                blocks.append(u)
            blocks.extend(get_assistant_text_blocks(rec))
            for text in blocks:
                yield from _emit_bans(rec, text)
                yield from _emit_failed_attempts(rec, text)


# ---------------------------------------------------------------------------
# Emission helpers
# ---------------------------------------------------------------------------


def _sentence_around(text: str, span_start: int, span_end: int) -> str:
    """Return the sentence containing ``[span_start:span_end]`` from ``text``."""
    # Walk back to the last sentence boundary.
    left = max(
        text.rfind(". ", 0, span_start),
        text.rfind("\n", 0, span_start),
        text.rfind("! ", 0, span_start),
        text.rfind("? ", 0, span_start),
    )
    start = 0 if left == -1 else left + 1
    # Walk forward to the next boundary.
    candidates = [
        i
        for i in (
            text.find(". ", span_end),
            text.find("\n", span_end),
            text.find("! ", span_end),
            text.find("? ", span_end),
        )
        if i != -1
    ]
    end = min(candidates) + 1 if candidates else len(text)
    return text[start:end].strip()


def _emit_bans(rec: RecordLike, text: str) -> Iterator[Extraction]:
    # Public-name patterns FIRST so an absolute-never match doesn't swallow them.
    seen_spans: list[tuple[int, int]] = []

    for m in _PUBLIC_NAME_RE.finditer(text):
        thing = m.group(1)
        public_clause = m.group(2).lower().replace(" ", " ")
        clause = "publicly" if "publicly" in public_clause else public_clause
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_ban(rec, thing, "context", sent, context_clause=clause)

    for m in _ABSOLUTE_NEVER_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        thing = m.group(1)
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_ban(rec, thing, "absolute", sent)

    for m in _NO_X_EVER_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        thing = m.group(1)
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_ban(rec, thing, "absolute", sent)

    for m in _PREFERENCE_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        thing = m.group(1)
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_ban(rec, thing, "preference", sent)

    for m in _STOP_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        thing = m.group(1)
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_ban(rec, thing, "context", sent)


def _emit_failed_attempts(rec: RecordLike, text: str) -> Iterator[Extraction]:
    seen_spans: list[tuple[int, int]] = []

    for m in _SWITCHED_FROM_RE.finditer(text):
        attempt = m.group(1)
        replaced_by = m.group(2)
        reason = m.group(3).strip()
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_failed(rec, attempt, replaced_by, reason, sent)

    for m in _SWITCHED_TO_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        replaced_by = m.group(1)
        attempt = m.group(2)
        reason = m.group(3).strip()
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_failed(rec, attempt, replaced_by, reason, sent)

    for m in _TRIED_FAILED_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        attempt = m.group(1)
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_failed(rec, attempt, None, sent, sent)

    for m in _MIGRATED_AWAY_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        attempt = m.group(1)
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_failed(rec, attempt, None, sent, sent)

    for m in _BROKEN_CO_RE.finditer(text):
        if _overlaps(m, seen_spans):
            continue
        attempt = m.group(1)
        if attempt.lower() in _BROKEN_STOPWORDS:
            continue
        sent = _sentence_around(text, m.start(), m.end())
        seen_spans.append((m.start(), m.end()))
        yield _make_failed(rec, attempt, None, sent, sent)


def _overlaps(m: re.Match, seen: list[tuple[int, int]]) -> bool:
    return any(not (m.end() <= s or m.start() >= e) for s, e in seen)


def _make_ban(
    rec: RecordLike,
    thing: str,
    strength: str,
    quote: str,
    context_clause: str | None = None,
) -> Extraction:
    ctx: dict = {
        "banned_thing": thing.lower(),
        "ban_strength": strength,
        "ban_text": quote,
    }
    if context_clause:
        ctx["context_clause"] = context_clause
    return Extraction(
        kind="ban",
        content=quote,
        session_id=getattr(rec, "session_id", "") or "",
        cwd=getattr(rec, "cwd", "") or "",
        ts=rec.ts,
        source_uuid=getattr(rec, "uuid", "") or "",
        score=_BASE_SCORE_BAN,
        context=ctx,
    )


def _make_failed(
    rec: RecordLike,
    attempt: str,
    replaced_by: str | None,
    reason: str | None,
    quote: str,
) -> Extraction:
    ctx: dict = {"attempt": attempt}
    if replaced_by:
        ctx["replaced_by"] = replaced_by
    if reason:
        ctx["reason"] = reason
    return Extraction(
        kind="failed_attempt",
        content=quote,
        session_id=getattr(rec, "session_id", "") or "",
        cwd=getattr(rec, "cwd", "") or "",
        ts=rec.ts,
        source_uuid=getattr(rec, "uuid", "") or "",
        score=_BASE_SCORE_FAILED,
        context=ctx,
    )


__all__ = ["Bans"]
