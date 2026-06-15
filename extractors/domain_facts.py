"""Signal #5 — Stable domain facts from the user.

The user sometimes drops declarative ground-truth in a single short line:
    - "its local lan"
    - "we have no users, nothing real, we are just still deving"
    - "the default github user on this machine should be operator-name /
       operator@example.com"

These are facts about the *world the user lives in*, not requests. They're
high-leverage for cross-session memory: a fresh agent that knows "this is
local LAN" or "the github identity is X" stops asking dumb confirmation
questions.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator

from extractors.base import DagLike, Extraction, Extractor, RecordLike, get_user_string

log = logging.getLogger(__name__)


_MAX_LEN = 200

# Imperative verbs that signal *requests*, not facts. We exclude these.
_IMPERATIVE_RE = re.compile(
    r"^(do|check|fix|run|fan|spin|install|deploy|make|use|add|remove|"
    r"update|delete|create)\b",
    re.IGNORECASE,
)

# Patterns that suggest the fact is identity / credential / infra-scoped and
# therefore belongs at `scope="global"` rather than per-project.
_GLOBAL_SCOPE_HINTS = re.compile(
    r"\b(github|gitlab|ssh|gpg|email|@[\w\.-]+\.[a-z]{2,}|"
    r"machine|laptop|workstation|hostname|wireguard|vpn|api[_-]?key|"
    r"token|credential|identity|username|account|default user)\b",
    re.IGNORECASE,
)

# Declarative-verb anchor: required for a string to count as a fact (filters
# out rambling musings like "why hasn't anyone made X"). Includes copulas /
# auxiliaries (`be`, `'s`, `its`, `it's`) so terse colloquial declaratives
# like "its local lan" still qualify.
_VERB_ANCHOR_RE = re.compile(
    r"(\bits\b|\bit'?s\b|'s\b|\b(is|are|was|were|be|been|being|"
    r"use[ds]?|have|has|had|run[s]?|host[s]?|"
    r"own[s]?|prefer[s]?|like[sd]?|want[s]?|need[s]?)\b)",
    re.IGNORECASE,
)

# Looser noun-like declarative anchor used for scoring boost.
_DECL_ANCHOR_SCORE_RE = re.compile(
    r"(our \w+ is|we use|we are|it is|they are|"
    r"\b(is|are|use|have|run|host)\b)",
    re.IGNORECASE,
)

_BASE_SCORE = 0.5
_DECL_ANCHOR_BONUS = 0.1
_GLOBAL_SCOPE_BONUS = 0.1
_TERSE_BONUS = 0.1
_TERSE_MAX_LEN = 80


class DomainFacts(Extractor):
    name = "domain_facts"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            text = get_user_string(rec)
            if text is None:
                continue
            stripped = text.strip()
            if len(stripped) >= _MAX_LEN:
                continue
            if "?" in stripped:
                continue
            if stripped.startswith("<"):
                continue
            first_char = stripped[:1]
            # Lowercase first letter == declarative, conversational, low-effort.
            # Title-case lines are almost always headings / code / proper nouns.
            if not first_char or not first_char.isalpha() or not first_char.islower():
                continue
            if _IMPERATIVE_RE.match(stripped):
                continue
            # Require at least one declarative-verb anchor — filters rambling
            # musings while keeping terse declaratives like "its local lan"
            # and "we have no users".
            if not _VERB_ANCHOR_RE.search(stripped):
                continue

            scope: str = "global" if _GLOBAL_SCOPE_HINTS.search(stripped) else "project"

            score = _BASE_SCORE
            if _DECL_ANCHOR_SCORE_RE.search(stripped):
                score += _DECL_ANCHOR_BONUS
            if scope == "global":
                score += _GLOBAL_SCOPE_BONUS
            if len(stripped) < _TERSE_MAX_LEN:
                score += _TERSE_BONUS
            score = min(score, 1.0)

            log.debug(
                "domain_facts: hit session=%s uuid=%s scope=%s score=%.2f",
                rec.session_id,
                rec.uuid,
                scope,
                score,
            )
            yield Extraction(
                kind="domain_fact",
                content=stripped,
                session_id=rec.session_id,
                cwd=rec.cwd,
                ts=rec.ts,
                source_uuid=rec.uuid,
                score=score,
                context={"scope_hint": scope},
                scope=scope,  # type: ignore[arg-type]
            )
