"""Signal — Model corrections.

The single highest-leverage extractor in the project. Captures every moment
the operator pushed back on what the model just did, paired with the
preceding assistant turn that triggered it. This becomes the feedback loop
that prevents future Claude sessions from re-making training-data-based
mistakes ("we never use OVH", "stop suggesting Stripe", etc.).

Distinct from :mod:`extractors.corrections` (the original lightweight
correction detector): this extractor uses a broader pattern set tuned by
research agent O8 across the real session corpus, scores severity per O9's
escalation research (profanity, insults, "I already said", terseness, and
back-to-back escalation chains), and always captures the rejected approach
plus the parent record uuid so downstream tools can reconstruct the
trigger -> rejection pair.
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
# Pattern set (research agent O8). All matched case-insensitively.
# ---------------------------------------------------------------------------

_CORRECTION_PATTERNS: list[str] = [
    r"^\s*no[,\s]",  # directive flip
    r"\bi\s+(told|said|asked)\s+you\b",  # restatement
    r"\b(already|how\s+many\s+times|you\s+forgot)\b",
    r"\bstop\s+(doing|using|adding|guessing|trying|thinking)\b",
    r"\b(don'?t|never)\s+(use|do|add|mention|recommend)\b",
    r"\b(wrong|that'?s\s+not|wtf)\b",
    r"\bwe\s+(never|don'?t|won'?t)\s+use\b",
    r"\bguessing\b",
    r"\bcheck\s+(our|the)\s+session\s+logs\b",  # cross-session memory appeal
    r"\bdrift(ing)?\b",  # drift callout
    r"\byou\s+(broke|are\s+lying)\b",  # severity-high
    r"\bnever\s+ever\b",  # strongest rule
]

_PATTERN_RE = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)


# Severity scoring helpers — order matches the spec.
_PROFANITY_RE = re.compile(r"\b(wtf|fuck|shit|damn)\b", re.IGNORECASE)
_INSULT_RE = re.compile(r"\b(idiot|dumb|lying|crazy|lost)\b", re.IGNORECASE)
_RESTATEMENT_RE = re.compile(
    r"\bi\s+(said|told)\b|\balready\b|\bnever\s+ever\b|\bforgot\b",
    re.IGNORECASE,
)


_MIN_LEN = 5
_MAX_LEN = 1500
_BASE_SCORE = 0.5


def _score(text: str, prev_was_correction: bool) -> float:
    """Severity score per spec — base 0.5, capped at 1.0."""
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
    return min(s, 1.0)


def _is_correction_text(text: str) -> bool:
    """True iff `text` looks like a model correction."""
    return bool(_PATTERN_RE.search(text))


class ModelCorrections(Extractor):
    name = "model_corrections"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        # We need lookback for escalation-chain detection. Iterate over a
        # materialized list rather than reading from the stream twice — the
        # orchestrator already hands us a list per its contract.
        rec_list = list(records)

        # Pre-compute which records are corrections so the escalation check
        # is O(1). We re-derive the text inside the loop to honor the same
        # skip rules a second time (cheap and keeps the code obvious).
        is_correction: dict[str, bool] = {}
        for r in rec_list:
            t = get_user_string(r)
            if t is None:
                is_correction[getattr(r, "uuid", "")] = False
                continue
            if len(t) < _MIN_LEN or len(t) > _MAX_LEN:
                is_correction[getattr(r, "uuid", "")] = False
                continue
            is_correction[getattr(r, "uuid", "")] = _is_correction_text(t)

        prev_user_was_correction = False

        for rec in rec_list:
            text = get_user_string(rec)
            if text is None:
                # Not a qualifying user string — but it might still be a user
                # record (e.g. tool_result). Only "user, string" turns can
                # update the escalation pointer.
                if getattr(rec, "type", None) == "user":
                    # Tool-result / meta turns reset escalation context.
                    prev_user_was_correction = False
                continue

            if len(text) < _MIN_LEN or len(text) > _MAX_LEN:
                prev_user_was_correction = False
                continue

            if not _is_correction_text(text):
                prev_user_was_correction = False
                continue

            # Walk back to the assistant turn that triggered the pushback.
            rejected_approach: str | None = None
            preceding_uuid: str | None = None
            if dag is not None:
                prev = _safe(dag.prev_assistant_turn, rec.uuid)
                if prev is not None:
                    preceding_uuid = getattr(prev, "uuid", None)
                    blocks = get_assistant_text_blocks(prev)
                    if blocks:
                        joined = "\n\n".join(blocks)
                        # Spec: only the last 400 chars — that's almost always
                        # the action statement / closing line that the user
                        # is reacting to.
                        rejected_approach = joined[-400:]

            severity = _score(text, prev_user_was_correction)

            ctx: dict = {
                "rejected_approach": rejected_approach,
                "correction": text,
                "preceding_uuid": preceding_uuid,
                "severity": severity,
            }

            log.debug(
                "model_corrections: hit session=%s uuid=%s severity=%.2f",
                rec.session_id,
                rec.uuid,
                severity,
            )
            yield Extraction(
                kind="model_correction",
                content=text,
                session_id=rec.session_id,
                cwd=rec.cwd,
                ts=rec.ts,
                source_uuid=rec.uuid,
                score=severity,
                context=ctx,
            )

            prev_user_was_correction = True


def _safe(fn, *args, **kwargs):
    """Call a DAG helper that may not exist or may raise; swallow and return None."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("dag helper %s raised %r", getattr(fn, "__name__", fn), e)
        return None
