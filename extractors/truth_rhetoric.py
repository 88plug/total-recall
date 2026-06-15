"""Signal — Truth-assertion rhetoric (per-category extractor).

Captures the seven distinct shapes of operator pushback that all collapse to
"the model is wrong and I am tired of repeating myself." Splitting these out
from :mod:`extractors.model_corrections` (which fires on the *aggregate*
signal) lets downstream tools answer category-shaped questions:

    - "Has the operator ever *quoted me back to myself* about X?"
    - "Has the operator ever appealed to the *session logs* about X?"
      (#1 friction in corpus — 44 instances.)
    - "Has the operator ever called me *drifting* or insulted my capability?"

Each match emits a ``truth_assertion`` extraction with
``context.category`` set to one of the seven labels enumerated below.

Category list (research agent O4):

    1. ``restatement``         — "no I said …"
    2. ``quote_back``          — "you said earlier …"
    3. ``standing_rule``       — "never (ever) use …"
    4. ``past_logs_appeal``    — "check our session logs"
    5. ``drift_callout``       — "you are drifting"
    6. ``capability_insult``   — "are you stupid?"
    7. ``verify_yourself_push``— "you verify", "ssh in and check"

Severity uses the same scoring as :mod:`extractors.model_corrections` plus a
flat +0.15 bump for the ``capability_insult`` category (those are the loudest
signal in the corpus and the most expensive to repeat).
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
# Categories — order matters: first matching category wins. Ordering is by
# specificity (most specific phrasing first) so e.g. "check our session logs"
# is classified as `past_logs_appeal` and not as a generic `standing_rule`.
# ---------------------------------------------------------------------------

CATEGORIES: tuple[str, ...] = (
    "past_logs_appeal",
    "quote_back",
    "restatement",
    "drift_callout",
    "capability_insult",
    "verify_yourself_push",
    "standing_rule",
)


_CATEGORY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "restatement": [
        re.compile(r"(?i)^\s*no[,\s]+i\s+said\b"),
        re.compile(r"(?i)\bno\s+i\s+(told|said|asked)\s+you\b"),
    ],
    "quote_back": [
        re.compile(
            r"(?i)\byou\s+(said|told\s+me)\s+(earlie?r|before|just\s+now|first)"
        ),
    ],
    "standing_rule": [
        re.compile(r"(?i)\bnever\s+(ever\s+)?(use|recommend|do|mention)"),
        re.compile(r"(?i)\bwe\s+(will|won'?t)\s+never\s+use"),
    ],
    "past_logs_appeal": [
        re.compile(
            r"(?i)\b(check|read)\s+(our|the)\s+"
            r"(session\s+logs?|previous\s+claude\s+code|past\s+sessions?)\b"
        ),
    ],
    "drift_callout": [
        re.compile(r"(?i)\byou\s+are\s+drifting\b"),
        re.compile(r"(?i)\bstop\s+thinking\s+like\b"),
        re.compile(r"(?i)\b(ensure\s+we\s+never\s+drift|drifted)\b"),
    ],
    "capability_insult": [
        re.compile(
            r"(?i)\b(are\s+you\s+stupid|did\s+you\s+not\s+read|cant\s+you\s+|"
            r"you\s+(dumb|stupid|idiot)|liek\s+an?\s+idiot)\b"
        ),
    ],
    "verify_yourself_push": [
        re.compile(r"(?i)\byou\s+verify\b"),
        re.compile(r"(?i)\b(login|ssh)\s+(to|into).*(validate|check)\b"),
        re.compile(r"(?i)\bget\s+smarter\s+about\b"),
    ],
}


# ---------------------------------------------------------------------------
# Severity scoring — identical to model_corrections, with the +0.15 bump for
# capability_insult applied at the category gate (not inside the generic
# scoring helpers, so the bump cannot accidentally double-count when a single
# message hits both `capability_insult` and another category).
# ---------------------------------------------------------------------------

_PROFANITY_RE = re.compile(r"\b(wtf|fuck|shit|damn)\b", re.IGNORECASE)
_INSULT_RE = re.compile(r"\b(idiot|dumb|lying|crazy|lost)\b", re.IGNORECASE)
_RESTATEMENT_RE = re.compile(
    r"\bi\s+(said|told)\b|\balready\b|\bnever\s+ever\b|\bforgot\b",
    re.IGNORECASE,
)

_MIN_LEN = 5
_MAX_LEN = 1500
_BASE_SCORE = 0.5
_CAPABILITY_INSULT_BUMP = 0.15


def _score(text: str, category: str, prev_was_correction: bool) -> float:
    """Severity score per spec — base 0.5, +0.15 if category is capability_insult, capped at 1.0."""
    s = _BASE_SCORE
    if _PROFANITY_RE.search(text):
        s += 0.2
    if _INSULT_RE.search(text):
        s += 0.15
    if _RESTATEMENT_RE.search(text):
        s += 0.1
    if len(text) < 50:
        s += 0.1
    if prev_was_correction:
        s += 0.1
    if category == "capability_insult":
        s += _CAPABILITY_INSULT_BUMP
    return min(s, 1.0)


def _classify(text: str) -> str | None:
    """Return the first matching category for `text`, or ``None``."""
    for cat in CATEGORIES:
        for pat in _CATEGORY_PATTERNS[cat]:
            if pat.search(text):
                return cat
    return None


class TruthRhetoric(Extractor):
    name = "truth_rhetoric"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        rec_list = list(records)

        # Pre-classify so the escalation pointer can update without re-running
        # the category scan twice.
        classification: dict[str, str | None] = {}
        for r in rec_list:
            t = get_user_string(r)
            if t is None or len(t) < _MIN_LEN or len(t) > _MAX_LEN:
                classification[getattr(r, "uuid", "")] = None
                continue
            classification[getattr(r, "uuid", "")] = _classify(t)

        prev_user_was_assertion = False

        for rec in rec_list:
            text = get_user_string(rec)
            if text is None:
                if getattr(rec, "type", None) == "user":
                    prev_user_was_assertion = False
                continue

            if len(text) < _MIN_LEN or len(text) > _MAX_LEN:
                prev_user_was_assertion = False
                continue

            category = _classify(text)
            if category is None:
                prev_user_was_assertion = False
                continue

            preceding_uuid: str | None = None
            preceding_excerpt: str | None = None
            if dag is not None:
                prev = _safe(dag.prev_assistant_turn, rec.uuid)
                if prev is not None:
                    preceding_uuid = getattr(prev, "uuid", None)
                    blocks = get_assistant_text_blocks(prev)
                    if blocks:
                        joined = "\n\n".join(blocks)
                        preceding_excerpt = joined[-400:]

            severity = _score(text, category, prev_user_was_assertion)

            ctx: dict = {
                "category": category,
                "preceding_assistant_uuid": preceding_uuid,
                "preceding_assistant_text_excerpt": preceding_excerpt,
                "severity": severity,
            }

            log.debug(
                "truth_rhetoric: hit session=%s uuid=%s category=%s severity=%.2f",
                rec.session_id,
                rec.uuid,
                category,
                severity,
            )
            yield Extraction(
                kind="truth_assertion",
                content=text,
                session_id=rec.session_id,
                cwd=rec.cwd,
                ts=rec.ts,
                source_uuid=rec.uuid,
                score=severity,
                context=ctx,
            )

            prev_user_was_assertion = True


def _safe(fn, *args, **kwargs):
    """Call a DAG helper that may not exist or may raise; swallow and return None."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("dag helper %s raised %r", getattr(fn, "__name__", fn), e)
        return None


__all__ = ["TruthRhetoric", "CATEGORIES"]
