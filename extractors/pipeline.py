"""Orchestrator: run every extractor against a record stream, scrubbing secrets.

Each extractor is lazy; the orchestrator chains them so the caller can iterate
once over a `list[Record]` and receive every kind of `Extraction` interleaved.
The scrubber runs on `content` plus every string field in `context` so leaks
don't sneak through via the auxiliary fields.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace

from extractors.away_summaries import AwaySummaries
from extractors.bans import Bans
from extractors.base import DagLike, Extraction, Extractor, RecordLike
from extractors.corrections import Corrections
from extractors.decisions import Decisions
from extractors.domain_facts import DomainFacts
from extractors.goals import Goals
from extractors.model_corrections import ModelCorrections
from extractors.progress import Progress
from extractors.secrets import scrub_secrets
from extractors.self_corrections import SelfCorrections
from extractors.standing_decisions import StandingDecisions
from extractors.truth_rhetoric import TruthRhetoric

log = logging.getLogger(__name__)


ALL_EXTRACTORS: list[Extractor] = [
    Corrections(),
    Decisions(),
    SelfCorrections(),
    Progress(),
    DomainFacts(),
    AwaySummaries(),
    # v0.3 operator-aware extractors (research phases O1-O10 + impl I1-I10)
    # I2 — the highest-leverage extractor: pairs user pushback with rejected approach
    ModelCorrections(),
    # I5 — provider-a > provider-b, billing-provider-a > billing-provider-b, etc.
    StandingDecisions(),
    Bans(),  # I6 — provider/tool/pattern bans + failed attempts
    Goals(),  # I3 — per-project goal stack with status state machine
    TruthRhetoric(),  # I9 — 7-category truth-assertion taxonomy
]


Scrubber = Callable[[str], str]


def _scrub_obj(obj, scrubber: Scrubber):
    """Recursively scrub every string buried in `obj`.

    Walks dicts and lists so extractors that put structured payloads
    (e.g. `{"diff": {"before": "...", "after": "..."}}`) don't leak secrets
    through nested fields.
    """
    if isinstance(obj, str):
        return scrubber(obj)
    if isinstance(obj, dict):
        return {k: _scrub_obj(v, scrubber) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj(x, scrubber) for x in obj]
    return obj


def _scrub_extraction(ext: Extraction, scrubber: Scrubber) -> Extraction:
    """Return a copy of `ext` with `content` and all nested `context` strings scrubbed."""
    new_content = _scrub_obj(ext.content, scrubber)
    new_context = _scrub_obj(ext.context, scrubber)
    if new_content == ext.content and new_context == ext.context:
        return ext
    return replace(ext, content=new_content, context=new_context)


def run_all(
    records: list[RecordLike],
    dag: DagLike | None = None,
    scrubber: Scrubber = scrub_secrets,
    extractors: Iterable[Extractor] | None = None,
) -> Iterator[Extraction]:
    """Yield every `Extraction` produced by every registered extractor.

    `records` is taken as a `list` (the standard `Extractor` API accepts any
    `Iterable`, but the orchestrator passes the same sequence to multiple
    extractors so it must be re-iterable — a list is the simplest guarantee).
    """
    if extractors is None:
        extractors = ALL_EXTRACTORS
    for ex in extractors:
        n = 0
        try:
            for raw in ex.extract(records, dag=dag):
                n += 1
                yield _scrub_extraction(raw, scrubber)
        except Exception:  # pragma: no cover - keep pipeline alive on per-extractor bugs
            log.exception("extractor %s blew up; continuing", ex.name)
        finally:
            log.debug("extractor=%s emitted=%d", ex.name, n)


__all__ = ["ALL_EXTRACTORS", "run_all"]
