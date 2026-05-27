"""Ranked signal extractors for Claude Code session transcripts.

Each extractor consumes parsed `Record` objects (see `lib.schema`) and yields
`Extraction` candidates representing structured memory the recall layer can
later index, score, and surface back into future sessions.

Public surface:
    - `Extraction` dataclass (extractors.base)
    - `Extractor` ABC (extractors.base)
    - `ALL_EXTRACTORS`, `run_all` (extractors.pipeline)
    - `scrub_secrets` (extractors.secrets)
"""

from extractors.base import Extraction, Extractor
from extractors.pipeline import ALL_EXTRACTORS, run_all
from extractors.secrets import scrub_secrets

__all__ = [
    "Extraction",
    "Extractor",
    "ALL_EXTRACTORS",
    "run_all",
    "scrub_secrets",
]
