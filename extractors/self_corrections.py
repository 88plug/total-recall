"""Signal #3 — Assistant self-corrections.

Phrases like "You're right", "Good catch", "I should have..." mark moments
where the assistant updated its model of the problem. Pairing each with the
preceding user turn gives us a `(trigger, correction)` pair — high signal for
future-self about what kind of pushback actually moves the model.
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


# Case-sensitive on the leading token — matches the natural sentence-case the
# assistant produces. Anchored at start to avoid mid-paragraph false positives.
_SELF_CORRECTION_RE = re.compile(
    r"^(You'?re right|Good (call|catch|point)|I should have|I was wrong|"
    r"Let me actually (verify|check|look)|My mistake|Apologies, that was wrong)\b"
)

# Most-explicit self-correction phrase — earns the highest bonus.
_YOURE_RIGHT_RE = re.compile(r"^You'?re right\b")

# Trigger words on the preceding user turn signalling pushback.
_USER_TRIGGER_RE = re.compile(r"\b(no|wrong|actually)\b", re.IGNORECASE)

_BASE_SCORE = 0.65
_YOURE_RIGHT_BONUS = 0.15
_USER_TRIGGER_BONUS = 0.05


class SelfCorrections(Extractor):
    name = "self_corrections"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            for block in get_assistant_text_blocks(rec):
                # Per-paragraph match — assistants often open one paragraph
                # with "You're right" inside a longer reply.
                paragraphs = [p.strip() for p in block.split("\n\n") if p.strip()]
                for para in paragraphs:
                    if not _SELF_CORRECTION_RE.match(para):
                        continue
                    trigger: str | None = None
                    if dag is not None:
                        # Walk up the DAG to the nearest user turn.
                        parent_uuid = getattr(rec, "parent_uuid", None)
                        cursor = parent_uuid
                        hops = 0
                        while cursor and hops < 6:
                            parent = _safe(dag.get, cursor)
                            if parent is None:
                                break
                            if getattr(parent, "type", None) == "user":
                                trigger = get_user_string(parent) or _coerce_text(parent)
                                break
                            cursor = getattr(parent, "parent_uuid", None)
                            hops += 1
                    ctx: dict = {}
                    if trigger:
                        ctx["trigger"] = trigger

                    score = _BASE_SCORE
                    if _YOURE_RIGHT_RE.match(para):
                        score += _YOURE_RIGHT_BONUS
                    if trigger and _USER_TRIGGER_RE.search(trigger):
                        score += _USER_TRIGGER_BONUS
                    score = min(score, 1.0)

                    log.debug(
                        "self_corrections: hit session=%s uuid=%s score=%.2f",
                        rec.session_id,
                        rec.uuid,
                        score,
                    )
                    yield Extraction(
                        kind="self_correction",
                        content=para,
                        session_id=rec.session_id,
                        cwd=rec.cwd,
                        ts=rec.ts,
                        source_uuid=rec.uuid,
                        score=score,
                        context=ctx,
                    )


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # pragma: no cover - defensive
        return None


def _coerce_text(rec: RecordLike) -> str | None:
    t = getattr(rec, "text", None)
    if isinstance(t, str):
        return t
    c = getattr(rec, "content", None)
    if isinstance(c, str):
        return c
    return None
