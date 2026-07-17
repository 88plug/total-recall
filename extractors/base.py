"""Base types for signal extractors.

`Record` and `Dag` come from `lib.schema` / `lib.dag` once those modules exist
in this branch. Until then we depend on them only via `TYPE_CHECKING` plus a
runtime `Protocol` stub so the package imports cleanly on a bare branch.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lib.dag import Dag  # noqa: F401
    from lib.schema import Record  # noqa: F401


log = logging.getLogger(__name__)


@runtime_checkable
class RecordLike(Protocol):
    """Minimal duck-typed shape extractors need from a parsed session record.

    The real `lib.schema.Record` is expected to satisfy this; tests and any
    bare-branch consumers can pass simple objects (dataclasses, SimpleNamespace,
    etc.) as long as they expose these attributes.
    """

    type: str
    uuid: str
    parent_uuid: str | None
    session_id: str
    cwd: str
    ts: datetime
    role: str | None
    content_kind: str | None
    content: Any
    text: str | None
    is_meta: bool
    is_compact_summary: bool
    is_sidechain: bool
    subtype: str | None
    payload: dict | None


@runtime_checkable
class DagLike(Protocol):
    """Subset of `lib.dag.Dag` we need: lookups by uuid and parent/child walks."""

    def get(self, uuid: str) -> RecordLike | None: ...
    def parent_of(self, uuid: str) -> RecordLike | None: ...
    def next_user_turn(self, uuid: str, within: int = 5) -> RecordLike | None: ...
    def prev_assistant_turn(self, uuid: str) -> RecordLike | None: ...


@dataclass
class Extraction:
    """A single memory candidate produced by an extractor."""

    kind: str
    content: str
    session_id: str
    cwd: str
    ts: datetime
    source_uuid: str
    score: float
    context: dict = field(default_factory=dict)
    scope: Literal["project", "global"] = "project"

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            # Clamp rather than raise; scoring heuristics can overshoot
            # and we'd rather keep the candidate than lose it.
            self.score = max(0.0, min(1.0, self.score))


class Extractor(ABC):
    """Stateless transform: records -> extraction candidates.

    Implementations must be lazy (return an iterator) so the orchestrator can
    stream over multi-GB session corpora without slurping.
    """

    name: str = "extractor"

    @abstractmethod
    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        """Yield zero or more `Extraction`s from the given records."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared filtering helpers — used by every extractor so the "skip" rules are
# defined exactly once.
# ---------------------------------------------------------------------------

_MIN_LEN = 3
_MAX_LEN = 50_000
_SKIP_PREFIXES = (
    "<task-notification>",
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)


def is_meta_or_compact(rec: RecordLike) -> bool:
    return bool(getattr(rec, "is_meta", False)) or bool(getattr(rec, "is_compact_summary", False))


def is_skipped_user_string(text: str) -> bool:
    """Universal skip rules for user-string content (notifications, slash-cmds)."""
    if not isinstance(text, str):
        return True
    stripped = text.lstrip()
    if any(stripped.startswith(p) for p in _SKIP_PREFIXES):
        return True
    return bool(len(stripped) < _MIN_LEN or len(stripped) > _MAX_LEN)


def get_user_string(rec: RecordLike) -> str | None:
    """Return the user-string payload if this record qualifies, else None."""
    if getattr(rec, "type", None) != "user":
        return None
    if getattr(rec, "content_kind", None) != "string":
        return None
    if is_meta_or_compact(rec):
        return None
    text = getattr(rec, "text", None)
    if text is None:
        # Some Record impls only expose .content
        c = getattr(rec, "content", None)
        if isinstance(c, str):
            text = c
    if not isinstance(text, str):
        return None
    if is_skipped_user_string(text):
        return None
    return text


def get_assistant_text_blocks(rec: RecordLike) -> list[str]:
    """Return all `text`-typed blocks from an assistant turn (excludes thinking)."""
    if getattr(rec, "type", None) != "assistant":
        return []
    if is_meta_or_compact(rec):
        return []
    blocks: list[str] = []
    content = getattr(rec, "content", None)
    # Case A: pre-flattened single .text str
    text_attr = getattr(rec, "text", None)
    if isinstance(text_attr, str) and text_attr.strip():
        blocks.append(text_attr)
    # Case B: raw content array of {type, text}
    if isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text":
                t = blk.get("text")
                if isinstance(t, str) and t.strip():
                    blocks.append(t)
    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for b in blocks:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out
