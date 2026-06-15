"""Bidirectional satisfaction extractor.

Walks session DAGs (``parentUuid`` linkage) to pair each operator reaction
(praise OR frustration) with the assistant-turn shape that preceded it, then
aggregates a cross-tab of (reaction, ai_behavior) → count.

Reaction classification
-----------------------
User turns are classified into one of eight reaction categories:

    praise_quality    — "perfect", "exactly", "nice", "great", "love it"
    praise_launch     — short launch words (≤3 words): "go", "yes", "ship it", …
    silent_accept     — ≤2-word turn that is NOT a praise or frustration signal
    frustration_drift — drift/off-track language
    frustration_broke — "you broke it", "broke the css", …
    frustration_wtf   — strong profanity
    frustration_scope — "didn't ask", "too much", "scope creep", …
    frustration_verbosity — "too long", "shorter", "tl;dr", …

Silent-accept rationale
-----------------------
The cleanest heuristic is: ≤2 words, not in any other reaction category.
The "next-turn builds on the result" check in the spec is appealing but
requires a second forward pass and introduces noise when sessions branch.
The proxy (≤2 words, uncategorised) captures the same population —
an operator who types only "ok" or "yep" after a lengthy assistant turn is
expressing passive acceptance, not neutral non-reaction.  False-positive rate
is low because every other reaction category is checked first; only the
truly ambiguous short turns fall into silent_accept.

Assistant-behavior classification
----------------------------------
Assessed on the parent assistant turn of the reaction:

    tool_call_brief   — contains tool_use blocks, total text < 200 chars
    tool_call_verbose — contains tool_use blocks, total text ≥ 200 chars
    long_prose        — no tool_use, total text ≥ 400 chars
    mid_prose         — no tool_use, 100 ≤ total < 400 chars
    short_ack         — no tool_use, total < 100 chars
    confirmation_request — ends with '?' and last sentence has "should I" /
                           "proceed" / "ok to" / "want me to"

Design constraints
------------------
* stdlib + sqlite3 only.  No LLM.  No operator-specific literals.
* Read-only on session JSONL.
* Streaming: processes one line at a time inside ``_load_dag``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "extract_satisfaction",
    "extract_satisfaction_incremental",
    "extract_satisfaction_from_records",
    "classify_reaction",
    "classify_ai_behavior",
]


# ---------------------------------------------------------------------------
# Reaction patterns
# ---------------------------------------------------------------------------

_RE_PRAISE_QUALITY = re.compile(
    r"\b(perfect|exactly|nice|great|love it)\b", re.IGNORECASE
)

_RE_PRAISE_LAUNCH = re.compile(
    r"^(send it|do it|go|ship it|yes|continue|proceed|yep|ok)$",
    re.IGNORECASE,
)

_RE_FRUSTRATION_DRIFT = re.compile(
    r"\bdrifting\b|\bdrift\b|you'?re drift|fix the drift|\boff track\b|\bdiverging\b",
    re.IGNORECASE,
)

_RE_FRUSTRATION_BROKE = re.compile(
    r"\byou (broke|missed)\b|\bbroke (it|css|dns|things)\b",
    re.IGNORECASE,
)

_RE_FRUSTRATION_WTF = re.compile(
    r"\b(wtf|ffs|fuck|shit|bullshit)\b", re.IGNORECASE
)

_RE_FRUSTRATION_SCOPE = re.compile(
    r"\bdidn'?t ask\b|\bstick to\b|\btoo much\b|\bout of scope\b|\bscope creep\b"
    r"|no,\s+I (said|meant) (all|entire|every|whole)\b",
    re.IGNORECASE,
)

_RE_FRUSTRATION_VERBOSITY = re.compile(
    r"\btoo long\b|\bshorter\b|\bless\b|\btl;?dr\b|\bsummari[sz]ing too much\b",
    re.IGNORECASE,
)

_RE_CONFIRMATION_REQUEST = re.compile(
    r"(should I|proceed|ok to|want me to)",
    re.IGNORECASE,
)


def classify_reaction(text: str) -> str | None:
    """Return the reaction category for a user turn, or ``None`` if no match.

    Precedence matters: frustration signals are checked before praise before
    silent_accept so the most informative label wins.
    """
    stripped = text.strip()
    word_count = len(stripped.split()) if stripped else 0

    if _RE_FRUSTRATION_DRIFT.search(stripped):
        return "frustration_drift"
    if _RE_FRUSTRATION_BROKE.search(stripped):
        return "frustration_broke"
    if _RE_FRUSTRATION_WTF.search(stripped):
        return "frustration_wtf"
    if _RE_FRUSTRATION_SCOPE.search(stripped):
        return "frustration_scope"
    if _RE_FRUSTRATION_VERBOSITY.search(stripped):
        return "frustration_verbosity"

    if _RE_PRAISE_QUALITY.search(stripped):
        return "praise_quality"

    if word_count <= 3 and _RE_PRAISE_LAUNCH.match(stripped):
        return "praise_launch"

    if word_count <= 2 and word_count >= 1:
        return "silent_accept"

    return None


def classify_ai_behavior(content_blocks: list[dict], full_text: str) -> str:
    """Classify the assistant turn shape from its content blocks and text.

    Parameters
    ----------
    content_blocks:
        The ``message.content`` list from the assistant record.
    full_text:
        The concatenated text of all ``text``-type blocks (not thinking).
    """
    has_tool_use = any(b.get("type") == "tool_use" for b in content_blocks)
    text_len = len(full_text)

    if has_tool_use:
        return "tool_call_brief" if text_len < 200 else "tool_call_verbose"

    if text_len >= 400:
        return "long_prose"
    if text_len >= 100:
        # Check for confirmation request before labelling mid_prose.
        stripped = full_text.rstrip()
        if stripped.endswith("?"):
            # Extract last sentence (simple: after last period / newline).
            last_sentence_match = re.search(r"[.!\n]([^.!\n]+\?)\s*$", stripped)
            last_sentence = (
                last_sentence_match.group(1) if last_sentence_match else stripped
            )
            if _RE_CONFIRMATION_REQUEST.search(last_sentence):
                return "confirmation_request"
        return "mid_prose"
    if text_len > 0:
        stripped = full_text.rstrip()
        if stripped.endswith("?"):
            last_sentence_match = re.search(r"[.!\n]([^.!\n]+\?)\s*$", stripped)
            last_sentence = (
                last_sentence_match.group(1) if last_sentence_match else stripped
            )
            if _RE_CONFIRMATION_REQUEST.search(last_sentence):
                return "confirmation_request"
    return "short_ack"


# ---------------------------------------------------------------------------
# DAG loader
# ---------------------------------------------------------------------------


def _load_dag(path: Path) -> dict[str, dict]:
    """Read a session JSONL into a uuid→record dict (skip blank/bad lines)."""
    records: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                uid = rec.get("uuid")
                if uid:
                    records[uid] = rec
    except OSError as exc:
        log.debug("Cannot open %s: %s", path, exc)
    return records


def _extract_text(content_blocks: list[dict]) -> str:
    """Concatenate all non-thinking text blocks from an assistant message."""
    parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def _get_content_str(record: dict) -> str:
    """Return the plain-text content of a user turn."""
    msg = record.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _process_dag(records: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Walk the DAG and accumulate (reaction, ai_behavior) → count.

    Returns a nested dict: ``{reaction: {ai_behavior: count}}``.
    """
    counts: dict[str, dict[str, int]] = {}

    # Build parent→record lookup (keyed by uuid for O(1) parent lookup).
    # We need: for each user turn, find its parent assistant turn.
    for _uid, rec in records.items():
        if rec.get("type") != "user":
            continue

        # Skip sidechains (sub-agent turns) — they're not direct operator reactions.
        if rec.get("isSidechain"):
            continue

        user_text = _get_content_str(rec)
        if not user_text:
            continue

        reaction = classify_reaction(user_text)
        if reaction is None:
            continue

        # Walk up the parentUuid chain to find the nearest assistant turn.
        parent_uid = rec.get("parentUuid")
        assistant_rec: dict | None = None
        while parent_uid:
            candidate = records.get(parent_uid)
            if candidate is None:
                break
            if candidate.get("type") == "assistant":
                assistant_rec = candidate
                break
            parent_uid = candidate.get("parentUuid")

        if assistant_rec is None:
            continue

        msg = assistant_rec.get("message", {})
        content_blocks: list[dict] = msg.get("content", [])
        if not isinstance(content_blocks, list):
            content_blocks = []

        full_text = _extract_text(content_blocks)
        ai_behavior = classify_ai_behavior(content_blocks, full_text)

        ts_raw = rec.get("timestamp")
        with contextlib.suppress(TypeError, ValueError):
            int(ts_raw) if ts_raw is not None else None

        counts.setdefault(reaction, {})[ai_behavior] = (
            counts.get(reaction, {}).get(ai_behavior, 0) + 1
        )

    return counts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _merge_matrices(
    base: dict[str, dict[str, int]],
    delta: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    merged = {r: dict(bmap) for r, bmap in base.items()}
    for reaction, behaviors in delta.items():
        for ai_behavior, count in behaviors.items():
            merged.setdefault(reaction, {})[ai_behavior] = (
                merged.get(reaction, {}).get(ai_behavior, 0) + count
            )
    return merged


def _summarise(matrix: dict[str, dict[str, int]]) -> dict:
    """Compute top-line stats from the matrix and wrap into the profile dict."""
    _PRAISE = {"praise_quality", "praise_launch", "silent_accept"}
    _FRUSTRATION = {
        "frustration_drift", "frustration_broke", "frustration_wtf",
        "frustration_scope", "frustration_verbosity",
    }
    total_praise = sum(
        c for r, bmap in matrix.items() if r in _PRAISE for c in bmap.values()
    )
    total_frustration = sum(
        c for r, bmap in matrix.items() if r in _FRUSTRATION for c in bmap.values()
    )
    sample_size = total_praise + total_frustration
    return {
        "matrix": matrix,
        "sample_size": sample_size,
        "total_praise_count": total_praise,
        "total_frustration_count": total_frustration,
        "updated_ts": None,
    }


def extract_satisfaction(jsonl_paths: Iterable[Path | str]) -> dict:
    """Full-pass extraction: walk all provided JSONL files.

    Parameters
    ----------
    jsonl_paths:
        Iterable of paths to session JSONL files.

    Returns
    -------
    dict — profile dict suitable for
    :func:`index.satisfaction.persist_satisfaction`.
    """
    matrix: dict[str, dict[str, int]] = {}
    for p in jsonl_paths:
        records = _load_dag(Path(p))
        delta = _process_dag(records)
        matrix = _merge_matrices(matrix, delta)
    return _summarise(matrix)


def extract_satisfaction_incremental(
    records: Iterable[dict],
    existing: dict,
) -> dict:
    """Additive merge: process a stream of pre-loaded records and merge into existing profile.

    Parameters
    ----------
    records:
        Iterable of already-parsed JSON record dicts (from a single session or
        a batch).  They must all have ``uuid`` keys for DAG traversal.
    existing:
        The profile dict returned by a prior call to
        :func:`extract_satisfaction` or :func:`extract_satisfaction_incremental`.
        May be empty (``{}``).

    Returns
    -------
    dict — updated profile dict (accumulates; counts only ever increase).
    """
    rec_map: dict[str, dict] = {}
    for rec in records:
        uid = rec.get("uuid")
        if uid:
            rec_map[uid] = rec

    delta = _process_dag(rec_map)
    base_matrix: dict[str, dict[str, int]] = existing.get("matrix", {})
    merged = _merge_matrices(base_matrix, delta)
    return _summarise(merged)


def _record_to_dag_dict(rec: Any) -> dict | None:
    """Convert a :class:`lib.schema.Record` instance to the raw-dict shape
    that :func:`_process_dag` expects.

    ``_process_dag`` was written against Claude Code JSONL dicts and uses
    ``rec.get("type")``, ``rec.get("uuid")``, ``rec.get("parentUuid")``,
    ``rec.get("isSidechain")``, and ``rec.get("message", {})`` for content.
    This adapter bridges the normalized Record dataclass to that shape.

    Returns ``None`` for records with no uuid (not useful for DAG traversal).
    """
    uid = getattr(rec, "uuid", None)
    if not uid:
        return None

    rec_type = getattr(rec, "type", "")

    # Reconstruct the ``message`` dict from Record fields so _process_dag
    # can use its existing _get_content_str / _extract_text helpers.
    if rec_type == "user":
        text = getattr(rec, "text", None) or ""
        message: dict = {"role": "user", "content": text}
    elif rec_type == "assistant":
        # Rebuild content blocks list from Block dataclass instances.
        raw_blocks: list[dict] = []
        for blk in (getattr(rec, "content", None) or []):
            btype = getattr(blk, "type", "")
            if btype == "text":
                raw_blocks.append({"type": "text", "text": getattr(blk, "text", "") or ""})
            elif btype == "thinking":
                raw_blocks.append(
                    {"type": "thinking", "thinking": getattr(blk, "thinking", "") or ""}
                )
            elif btype == "tool_use":
                tu = getattr(blk, "tool_use", None)
                raw_blocks.append({
                    "type": "tool_use",
                    "id": getattr(tu, "id", "") if tu else "",
                    "name": getattr(tu, "name", "") if tu else "",
                    "input": getattr(tu, "input", {}) if tu else {},
                })
            else:
                raw_blocks.append(getattr(blk, "raw", None) or {"type": btype})
        message = {"role": "assistant", "content": raw_blocks}
    else:
        message = {}

    return {
        "type": rec_type,
        "uuid": uid,
        "parentUuid": getattr(rec, "parent_uuid", None),
        "isSidechain": bool(getattr(rec, "is_sidechain", False)),
        "message": message,
        "timestamp": None,
    }


def extract_satisfaction_from_records(
    records: Iterable[Any],
    existing: dict | None = None,
) -> dict:
    """Full or incremental satisfaction extraction from a multi-source record stream.

    Accepts :class:`lib.schema.Record` instances (as yielded by
    :func:`lib.sources.collect.iter_all_source_records`) and converts them to
    the raw-dict shape required by :func:`_process_dag`.

    Parameters
    ----------
    records:
        Iterable of :class:`lib.schema.Record` objects OR raw Claude Code JSONL
        dicts.  Mixed streams are handled — each item is probed with
        ``isinstance(rec, dict)`` to decide whether conversion is needed.
    existing:
        Optional existing profile dict to merge into (same semantics as
        :func:`extract_satisfaction_incremental`).  Pass ``None`` or ``{}`` for
        a cold-pass extraction.

    Returns
    -------
    dict — profile dict suitable for
    :func:`index.satisfaction.persist_satisfaction`.
    """
    dag_dicts: list[dict] = []
    for rec in records:
        if isinstance(rec, dict):
            dag_dicts.append(rec)
        else:
            converted = _record_to_dag_dict(rec)
            if converted is not None:
                dag_dicts.append(converted)

    return extract_satisfaction_incremental(dag_dicts, existing or {})
