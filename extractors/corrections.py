"""Signal #1 — User corrections.

Short, sharp user turns that reject what the assistant just did. These are the
single richest source of *preferences* in a transcript: the user only bothers
to push back when the model's default was wrong.
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


# Verbs / interjections that mark a correction. Anchored to the start (allowing
# leading whitespace) so we don't match these words mid-sentence in long pastes.
_CORRECTION_RE = re.compile(
    r"^\s*(no|stop|wait|don'?t|actually|nope|hold on|i said|i told you|"
    r"fuck(ing)?|wrong|never)\b",
    re.IGNORECASE,
)

_PROFANITY_RE = re.compile(r"\bfuck(ing)?\b", re.IGNORECASE)
_I_SAID_RE = re.compile(r"\bi (said|told you)\b", re.IGNORECASE)

_MAX_LEN = 500
_BASE_SCORE = 0.7


class Corrections(Extractor):
    name = "corrections"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            text = get_user_string(rec)
            if text is None:
                continue
            if len(text) >= _MAX_LEN:
                continue
            if not _CORRECTION_RE.match(text):
                continue

            score = _BASE_SCORE
            if _PROFANITY_RE.search(text):
                score += 0.2
            if _I_SAID_RE.search(text):
                score += 0.1

            rejected_approach: str | None = None
            clarification: str | None = None
            if dag is not None:
                prev = _safe(dag.prev_assistant_turn, rec.uuid)
                if prev is not None:
                    rejected_blocks = get_assistant_text_blocks(prev)
                    if rejected_blocks:
                        rejected_approach = "\n\n".join(rejected_blocks)
                nxt = _safe(dag.next_user_turn, rec.uuid, within=5)
                if nxt is not None:
                    nxt_text = get_user_string(nxt)
                    if nxt_text:
                        clarification = nxt_text

            ctx: dict = {}
            if rejected_approach:
                ctx["rejected_approach"] = rejected_approach
            if clarification:
                ctx["clarification"] = clarification

            log.debug(
                "corrections: hit session=%s uuid=%s score=%.2f",
                rec.session_id,
                rec.uuid,
                score,
            )
            yield Extraction(
                kind="correction",
                content=text,
                session_id=rec.session_id,
                cwd=rec.cwd,
                ts=rec.ts,
                source_uuid=rec.uuid,
                score=score,
                context=ctx,
            )


def _safe(fn, *args, **kwargs):
    """Call a DAG helper that may not exist or may raise; swallow and return None."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("dag helper %s raised %r", getattr(fn, "__name__", fn), e)
        return None
