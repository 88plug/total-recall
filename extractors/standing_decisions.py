"""Signal — Standing operator decisions.

Distinct from ``extractors.decisions`` (which captures *assistant* turns
articulating a choice in-the-moment) this extractor watches for the
*operator's* enduring positions: provider preferences, billing rails,
tunnel choices, bans, reversals, and the dollar amount they burned to
learn each lesson. The output is small and high-leverage — the kind of
thing you want surfaced at SessionStart so future Claude doesn't argue
about something the user already decided three sessions ago.

Patterns come from research note O5. We deliberately keep them simple
regexes (no NLP) and normalize topics to a small canonical vocabulary so
queries like "what did we decide about cloud_provider?" stay cheap.

The extractor yields ``Extraction`` rows of kind ``"standing_decision"``
with a structured ``context`` blob. A downstream sink (the indexer, or a
custom hook) is expected to lift that blob into the
``standing_decisions`` table — see :mod:`index.decisions`. The extractor
itself does not touch SQLite, matching the pattern of every other
extractor in this package.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from typing import Any

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
# Topic normalization
# ---------------------------------------------------------------------------
#
# Map raw match tokens to a small canonical vocabulary. Anything we can't
# place lands in ``misc`` — better than dropping the signal entirely.

_TOPIC_MAP: dict[str, str] = {
    # cloud_provider
    "ovh": "cloud_provider",
    "hetzner": "cloud_provider",
    "aws": "cloud_provider",
    "gcp": "cloud_provider",
    "azure": "cloud_provider",
    "digitalocean": "cloud_provider",
    "linode": "cloud_provider",
    "vultr": "cloud_provider",
    # billing_rail
    "paypal": "billing_rail",
    "stripe": "billing_rail",
    "lemonsqueezy": "billing_rail",
    "paddle": "billing_rail",
    # tunnel
    "wireguard": "tunnel",
    "openvpn": "tunnel",
    "tailscale": "tunnel",
    "zerotier": "tunnel",
    "ipsec": "tunnel",
    # forge
    "github": "forge",
    "gitlab": "forge",
    "gitea": "forge",
    "bitbucket": "forge",
    # container_orchestrator
    "docker": "container_orchestrator",
    "k8s": "container_orchestrator",
    "kubernetes": "container_orchestrator",
    "podman": "container_orchestrator",
    "swarm": "container_orchestrator",
    "nomad": "container_orchestrator",
}

_GLOBAL_TOPICS = {
    "cloud_provider",
    "billing_rail",
    "tunnel",
    "forge",
    "container_orchestrator",
}


def normalize_topic(token: str | None) -> str:
    """Map a raw match token (e.g. ``'<provider>'``) → canonical topic."""
    if not token:
        return "misc"
    key = token.strip().lower()
    return _TOPIC_MAP.get(key, "misc")


def _is_known_token(token: str | None) -> bool:
    """True if the token is in our normalization vocabulary."""
    return bool(token) and token.strip().lower() in _TOPIC_MAP


# ---------------------------------------------------------------------------
# Patterns — from research note O5
# ---------------------------------------------------------------------------
#
# Each regex is paired with a "kind" tag so the downstream sink can decide
# whether to UPSERT, mark-as-reversed, or attach a money-burn delta. We
# keep them anchored loosely (`\b`) so they fire on natural phrasing.

# Explicit decisions
_CHOSE_RE = re.compile(
    r"\b(?:chose|going with|sticking with|switching to|moved to)\s+(\w[\w\.\-]*)",
    re.IGNORECASE,
)
_INSTEAD_OF_RE = re.compile(
    r"\b(\w[\w\.\-]*)\s+(?:instead of|rather than|over)\s+(\w[\w\.\-]*)",
    re.IGNORECASE,
)
_DOOR_RE = re.compile(r"\bDoor #(\d+)\b")

# Bans
_NEVER_RE = re.compile(
    r"(?i)never\s+(?:ever\s+)?(?:recommend|use|mention)"
    r"(?:\s+or\s+\w+)*\s+(\w[\w\.\-]*)"
)
_WE_DONT_RE = re.compile(r"(?i)\bwe\s+(?:never|don'?t|won'?t)\s+use\s+(\w[\w\.\-]*)")

# Reversals
_MIGRATED_AWAY_RE = re.compile(r"(?i)MIGRATED AWAY FROM\s+(\w[\w\.\-]*)")
_ABANDONED_RE = re.compile(r"(?i)\b(?:abandoned|dead|broken)\b.*?(\w[\w\.\-]*)")
_REVERTED_CANONICAL_RE = re.compile(r"(?i)canonical\s+\S+\s+(?:path|again)")
_SOFT_GUIDANCE_RE = re.compile(r"(?i)don'?t push this hard")

# Money burn
_MONEY_BURN_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s+(?:lost|burned|wasted)", re.IGNORECASE)

# Codified preferences
_WINS_BECAUSE_RE = re.compile(r"\b(\w[\w\.\-]*)\s+(?:wins|beats)\s+because", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class StandingDecisions(Extractor):
    """Yield one ``standing_decision`` Extraction per regex hit.

    The downstream sink is expected to UPSERT into ``standing_decisions``
    keyed on ``(topic, chose, scope)`` — see :mod:`index.decisions`. We
    populate ``context`` with the fields the sink needs:

    * ``topic`` (normalized),
    * ``chose`` / ``over`` (raw tokens),
    * ``rationale`` (a short window of surrounding text where available),
    * ``pattern`` (which regex fired — useful for debugging false positives),
    * ``scope`` (``"global"`` for known topics, else ``"project"`` so the
      sink can map to the session's cwd),
    * ``is_reversed`` / ``reversed_to`` when a reversal pattern fires,
    * ``money_burn_usd`` when a "$N lost" phrase fires.
    """

    name = "standing_decisions"

    def extract(
        self,
        records: Iterable[RecordLike],
        dag: DagLike | None = None,
    ) -> Iterator[Extraction]:
        for rec in records:
            blocks: list[str] = []
            user_text = get_user_string(rec)
            if user_text is not None:
                blocks.append(user_text)
            blocks.extend(get_assistant_text_blocks(rec))

            for block in blocks:
                yield from self._scan_block(rec, block)

    # -- per-block scan ----------------------------------------------------

    def _scan_block(self, rec: RecordLike, block: str) -> Iterator[Extraction]:
        # 1) Money burn — emit even without a paired chose, sink will
        #    attach it to the most-recent decision if it can.
        for m in _MONEY_BURN_RE.finditer(block):
            try:
                amt = float(m.group(1))
            except (TypeError, ValueError):
                continue
            yield self._mk(
                rec,
                content=_window(block, m.start(), m.end()),
                topic="misc",
                chose="(money_burn)",
                pattern="money_burn",
                money_burn_usd=amt,
            )

        # 2) Explicit "chose X" / "going with X" / etc.
        for m in _CHOSE_RE.finditer(block):
            chose = m.group(1)
            yield self._decision(
                rec,
                block,
                m,
                chose=chose,
                over=None,
                pattern="chose",
            )

        # 3) "X instead of Y" / "X rather than Y" / "X over Y"
        for m in _INSTEAD_OF_RE.finditer(block):
            chose, over = m.group(1), m.group(2)
            yield self._decision(rec, block, m, chose=chose, over=over, pattern="instead_of")

        # 4) Door #N — operator's enumerated-choice shorthand.
        for m in _DOOR_RE.finditer(block):
            yield self._mk(
                rec,
                content=_window(block, m.start(), m.end()),
                topic="misc",
                chose=f"Door #{m.group(1)}",
                pattern="door",
            )

        # 5) Bans: "never recommend X" / "we never use X"
        for m in _NEVER_RE.finditer(block):
            yield self._decision(
                rec,
                block,
                m,
                chose=f"BAN:{m.group(1)}",
                over=m.group(1),
                pattern="ban",
                ban_target=m.group(1),
            )
        for m in _WE_DONT_RE.finditer(block):
            yield self._decision(
                rec,
                block,
                m,
                chose=f"BAN:{m.group(1)}",
                over=m.group(1),
                pattern="ban_we",
                ban_target=m.group(1),
            )

        # 6) Reversals
        for m in _MIGRATED_AWAY_RE.finditer(block):
            target = m.group(1)
            yield self._decision(
                rec,
                block,
                m,
                chose=target,
                over=None,
                pattern="migrated_away",
                is_reversed=True,
                reversed_target=target,
            )
        for m in _ABANDONED_RE.finditer(block):
            target = m.group(1)
            yield self._decision(
                rec,
                block,
                m,
                chose=target,
                over=None,
                pattern="abandoned",
                is_reversed=True,
                reversed_target=target,
            )
        if _REVERTED_CANONICAL_RE.search(block):
            yield self._mk(
                rec,
                content=block[:240],
                topic="misc",
                chose="(reverted_canonical)",
                pattern="reverted_canonical",
                is_reversed=True,
            )
        if _SOFT_GUIDANCE_RE.search(block):
            yield self._mk(
                rec,
                content=block[:240],
                topic="misc",
                chose="(soft_guidance)",
                pattern="soft_guidance",
            )

        # 7) "X wins because" — codified preference w/ rationale.
        for m in _WINS_BECAUSE_RE.finditer(block):
            chose = m.group(1)
            # Rationale = up to ~120 chars after "because"
            tail = block[m.end() :].lstrip()
            rationale = tail.split("\n", 1)[0][:200].strip()
            yield self._decision(
                rec,
                block,
                m,
                chose=chose,
                over=None,
                pattern="wins_because",
                rationale_override=rationale,
            )

    # -- helpers -----------------------------------------------------------

    def _decision(
        self,
        rec: RecordLike,
        block: str,
        m: re.Match[str],
        *,
        chose: str,
        over: str | None,
        pattern: str,
        is_reversed: bool = False,
        reversed_target: str | None = None,
        ban_target: str | None = None,
        rationale_override: str | None = None,
    ) -> Extraction:
        # Topic comes from the strongest known token in the match.
        candidates = [t for t in (chose, over, ban_target) if t]
        topic = "misc"
        for c in candidates:
            t = normalize_topic(c)
            if t != "misc":
                topic = t
                break

        snippet = _window(block, m.start(), m.end())
        rationale = rationale_override
        if rationale is None:
            # Cheap rationale heuristic: keep the surrounding window — the
            # sink can refine.
            rationale = snippet if len(snippet) <= 240 else snippet[:240]

        ctx: dict[str, Any] = {
            "topic": topic,
            "chose": chose,
            "pattern": pattern,
        }
        if over:
            ctx["over"] = over
        if rationale:
            ctx["rationale"] = rationale
        if is_reversed:
            ctx["is_reversed"] = True
            if reversed_target:
                ctx["reversed_to"] = reversed_target
        ctx["scope"] = "global" if topic in _GLOBAL_TOPICS else "project"

        return Extraction(
            kind="standing_decision",
            content=snippet,
            session_id=rec.session_id,
            cwd=rec.cwd,
            ts=rec.ts,
            source_uuid=rec.uuid,
            score=0.7,
            context=ctx,
            scope="global" if ctx["scope"] == "global" else "project",
        )

    def _mk(
        self,
        rec: RecordLike,
        *,
        content: str,
        topic: str,
        chose: str,
        pattern: str,
        is_reversed: bool = False,
        money_burn_usd: float = 0.0,
    ) -> Extraction:
        ctx: dict = {
            "topic": topic,
            "chose": chose,
            "pattern": pattern,
            "scope": "global" if topic in _GLOBAL_TOPICS else "project",
        }
        if is_reversed:
            ctx["is_reversed"] = True
        if money_burn_usd:
            ctx["money_burn_usd"] = float(money_burn_usd)
        return Extraction(
            kind="standing_decision",
            content=content,
            session_id=rec.session_id,
            cwd=rec.cwd,
            ts=rec.ts,
            source_uuid=rec.uuid,
            score=0.6,
            context=ctx,
            scope="global" if ctx["scope"] == "global" else "project",
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _window(text: str, start: int, end: int, pad: int = 80) -> str:
    """Return up-to-``pad``-char context window around a regex hit."""
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    return text[a:b].strip()
