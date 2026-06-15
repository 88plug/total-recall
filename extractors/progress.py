"""Signal #4 — Progress markers.

Short status emissions from either the assistant ("Done.", "Shipped",
"Remaining:") or user-pasted status tables (`[x]` / `~80%` lines). These tell
the recall layer "where did the previous session end up?" — the headline
question for any cross-session memory surface.
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


# Each alternative carries its own terminator (period, colon, or word boundary)
# so we don't need a trailing `\b` — `\b` adjacent to non-word chars like `.`
# and `:` fails when the following char is also non-word (a space).
_PROGRESS_RE = re.compile(
    r"^(Done\.|All shipped\b|Shipped\b|Committed\b|Fixed\b|Still (broken|need)\b|"
    r"Remaining:|Next:|Wired up\b|Landing this\b)"
)

# User-pasted status-table line markers.
_PERCENT_RE = re.compile(r"~\d+\s*%")
_CHECKBOX_RE = re.compile(r"\[[ xX]\]")

# Per-marker score gradient (assistant paragraph starts).
_DONE_RE = re.compile(r"^Done\.")
_SHIPPED_COMMITTED_RE = re.compile(r"^(Shipped|Committed)\b")
_NEXT_REMAINING_RE = re.compile(r"^(Next:|Remaining:)")

_BASE_ASSISTANT_SCORE = 0.55
_DONE_BONUS = 0.2
_SHIPPED_COMMITTED_BONUS = 0.1
_NEXT_REMAINING_BONUS = 0.05

_BASE_USER_SCORE = 0.55
_CHECKBOX_BONUS_PER_MARKER = 0.1


class Progress(Extractor):
    name = "progress"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            # Assistant text-block paragraph starts.
            for block in get_assistant_text_blocks(rec):
                paragraphs = [p.strip() for p in block.split("\n\n") if p.strip()]
                for para in paragraphs:
                    if not _PROGRESS_RE.match(para):
                        continue
                    marker = para.split("\n", 1)[0][:120]

                    score = _BASE_ASSISTANT_SCORE
                    if _DONE_RE.match(para):
                        score += _DONE_BONUS
                    elif _SHIPPED_COMMITTED_RE.match(para):
                        score += _SHIPPED_COMMITTED_BONUS
                    elif _NEXT_REMAINING_RE.match(para):
                        score += _NEXT_REMAINING_BONUS
                    score = min(score, 1.0)

                    log.debug(
                        "progress: assistant marker session=%s uuid=%s marker=%r score=%.2f",
                        rec.session_id,
                        rec.uuid,
                        marker,
                        score,
                    )
                    yield Extraction(
                        kind="progress",
                        content=para,
                        session_id=rec.session_id,
                        cwd=rec.cwd,
                        ts=rec.ts,
                        source_uuid=rec.uuid,
                        score=score,
                        context={"marker": marker, "source": "assistant"},
                    )

            # User-pasted status tables.
            user_text = get_user_string(rec)
            if user_text is None:
                continue
            status_lines: list[str] = []
            checkbox_hits = 0
            for line in user_text.splitlines():
                cb = _CHECKBOX_RE.search(line)
                if _PERCENT_RE.search(line) or cb:
                    status_lines.append(line.rstrip())
                if cb:
                    checkbox_hits += 1
            if not status_lines:
                continue
            joined = "\n".join(status_lines)

            score = min(
                1.0,
                _BASE_USER_SCORE + _CHECKBOX_BONUS_PER_MARKER * checkbox_hits,
            )

            log.debug(
                "progress: user status table session=%s uuid=%s lines=%d score=%.2f",
                rec.session_id,
                rec.uuid,
                len(status_lines),
                score,
            )
            yield Extraction(
                kind="progress",
                content=joined,
                session_id=rec.session_id,
                cwd=rec.cwd,
                ts=rec.ts,
                source_uuid=rec.uuid,
                score=score,
                context={"marker": "status_table", "source": "user_paste"},
            )
