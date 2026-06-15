"""Signal #6 (bonus) — `system.subtype == "away_summary"` payloads.

These are narrative recaps the agent wrote *for itself* (e.g. during the
`amnesia` plugin's PostCompact handoff). They're already a curated digest of
the session-so-far, so we keep them whole rather than trying to re-mine them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator

from extractors.base import DagLike, Extraction, Extractor, RecordLike

log = logging.getLogger(__name__)


_BASE_SCORE = 0.75
_LONG_BONUS = 0.1
_LONG_THRESHOLD = 200
_ACTION_MARKER_RE = re.compile(r"(Next:|Goal:)")
_ACTION_BONUS = 0.05


class AwaySummaries(Extractor):
    name = "away_summaries"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            if getattr(rec, "type", None) != "system":
                continue
            if getattr(rec, "subtype", None) != "away_summary":
                continue
            payload = getattr(rec, "payload", None) or {}
            content = None
            if isinstance(payload, dict):
                content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            score = _BASE_SCORE
            if len(content) > _LONG_THRESHOLD:
                score += _LONG_BONUS
            if _ACTION_MARKER_RE.search(content):
                score += _ACTION_BONUS
            score = min(score, 1.0)

            log.debug(
                "away_summaries: hit session=%s uuid=%s len=%d score=%.2f",
                rec.session_id,
                rec.uuid,
                len(content),
                score,
            )
            yield Extraction(
                kind="away_summary",
                content=content,
                session_id=rec.session_id,
                cwd=rec.cwd,
                ts=rec.ts,
                source_uuid=rec.uuid,
                score=score,
                context={"source": "system.away_summary"},
            )
