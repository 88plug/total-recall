"""Signal #2 — Assistant decisions ("instead of / rather than").

Assistant turns where the model articulates a choice between alternatives.
These are gold for the *why* of past technical choices.
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
)

log = logging.getLogger(__name__)


_DECISION_RE = re.compile(
    r"\b(instead of|rather than|over (the )?other|chose .* because|"
    r"why .* not|Door #\d|going to use .* not)\b",
    re.IGNORECASE,
)

# High-confidence rationale phrases — score bonus when present in the matched
# sentence itself.
_HIGH_CONF_RE = re.compile(r"\b(instead of|rather than)\b", re.IGNORECASE)
_BECAUSE_RE = re.compile(r"\bbecause\b", re.IGNORECASE)

_BASE_SCORE = 0.6
_HIGH_CONF_BONUS = 0.15
_BECAUSE_BONUS = 0.1
_CONTEXT_BONUS_PER_SENT = 0.05
_CONTEXT_BONUS_CAP_SENTS = 3

# `chose X because Y` / `going to use X not Y` — best-effort structured parse.
_CHOSE_RE = re.compile(
    r"\bchose\s+(?P<chose>.+?)\s+because\s+(?P<rationale>.+?)(?:[\.\n]|$)",
    re.IGNORECASE | re.DOTALL,
)
_INSTEAD_RE = re.compile(
    r"\b(?P<chose>[^\.\n]{1,120}?)\s+instead of\s+(?P<over>[^\.\n]{1,120})",
    re.IGNORECASE,
)
_RATHER_RE = re.compile(
    r"\b(?P<chose>[^\.\n]{1,120}?)\s+rather than\s+(?P<over>[^\.\n]{1,120})",
    re.IGNORECASE,
)
_USING_NOT_RE = re.compile(
    r"\bgoing to use\s+(?P<chose>[^\.\n]{1,120}?)\s+not\s+(?P<over>[^\.\n]{1,120})",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    """Naive sentence splitter. `\\. ` is the assignment-specified delimiter."""
    # Normalise whitespace inside the splitter only; preserve newlines as soft breaks.
    chunks: list[str] = []
    for line in text.split("\n"):
        parts = line.split(". ")
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                chunks.append(part.strip() + ".")
            else:
                if part.strip():
                    chunks.append(part.strip())
    return chunks


def _surrounding_snippet(sentences: list[str], hit_idx: int) -> str:
    """1 sentence before, the hit, 2 sentences after — joined with spaces."""
    start = max(0, hit_idx - 1)
    end = min(len(sentences), hit_idx + 3)  # exclusive
    return " ".join(sentences[start:end]).strip()


class Decisions(Extractor):
    name = "decisions"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            for block in get_assistant_text_blocks(rec):
                if not _DECISION_RE.search(block):
                    continue
                sentences = _split_sentences(block)
                if not sentences:
                    continue
                for idx, sent in enumerate(sentences):
                    if not _DECISION_RE.search(sent):
                        continue
                    snippet = _surrounding_snippet(sentences, idx)
                    chose, over, rationale = _parse_decision(snippet)
                    ctx: dict = {"snippet": snippet}
                    if chose:
                        ctx["chose"] = chose
                    if over:
                        ctx["over"] = over
                    if rationale:
                        ctx["rationale"] = rationale

                    score = _BASE_SCORE
                    if _HIGH_CONF_RE.search(sent):
                        score += _HIGH_CONF_BONUS
                    # `because` clause "follows" — same sentence or one of the
                    # following sentences within the snippet window.
                    follow_end = min(len(sentences), idx + 3)
                    if any(
                        _BECAUSE_RE.search(sentences[k])
                        for k in range(idx, follow_end)
                    ):
                        score += _BECAUSE_BONUS
                    # +0.05 per sentence of surrounding context up to 3.
                    # `_surrounding_snippet` covers indices [idx-1, idx+2].
                    ctx_start = max(0, idx - 1)
                    ctx_end = min(len(sentences), idx + 3)
                    ctx_sents = max(0, (ctx_end - ctx_start) - 1)  # exclude hit
                    score += _CONTEXT_BONUS_PER_SENT * min(
                        ctx_sents, _CONTEXT_BONUS_CAP_SENTS
                    )

                    log.debug(
                        "decisions: hit session=%s uuid=%s chose=%r over=%r score=%.2f",
                        rec.session_id,
                        rec.uuid,
                        chose,
                        over,
                        score,
                    )
                    yield Extraction(
                        kind="decision",
                        content=snippet,
                        session_id=rec.session_id,
                        cwd=rec.cwd,
                        ts=rec.ts,
                        source_uuid=rec.uuid,
                        score=score,
                        context=ctx,
                    )


def _parse_decision(snippet: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort extraction of (chose, over, rationale). Any may be None."""
    chose = over = rationale = None
    m = _CHOSE_RE.search(snippet)
    if m:
        chose = m.group("chose").strip(" ,.;:")
        rationale = m.group("rationale").strip(" ,.;:")
    if not chose or not over:
        m2 = _INSTEAD_RE.search(snippet) or _RATHER_RE.search(snippet) or _USING_NOT_RE.search(
            snippet
        )
        if m2:
            chose = chose or m2.group("chose").strip(" ,.;:")
            over = over or m2.group("over").strip(" ,.;:")
    return chose, over, rationale
