"""Shared payload bounds for every MCP tool surface.

``tools.py`` grew a careful bounding layer after a single 172k-char message
blew up the stdio JSON-RPC pipe. None of the 14 modules under
``mcp_server/extras/`` ever used it, so every tool that reads a structured
table returned its rows verbatim. Once those tables actually filled,
``get_operator_profile`` came back at 235,725 chars and
``list_standing_decisions`` at 101,061 — both over the client's token
ceiling, so the caller got an error instead of an answer.

The helpers live here rather than in ``tools.py`` because ``server.py``
imports the extras modules, so an extra importing ``tools`` would close an
import cycle. This module imports nothing from the package.
"""

from __future__ import annotations

import json
from typing import Any

# Per-field cap for bulky free text inside a single row.
MAX_HIT_TEXT_CHARS = 1500

# Field names treated as free text worth truncating.
TEXT_KEYS = frozenset(
    {
        "text",
        "content",
        "value",
        "preview",
        "body",
        "snippet",
        "ban_text",
        "rationale",
        "reason",
        "goal_text",
        "correction",
        "definition",
        "narrative",
    }
)

# Upper bound on a caller-supplied ``limit``.
MAX_TOOL_LIMIT = 50

# Aggregate response budget, in bytes of serialized JSON.
#
# Was 256_000, which is well past what an MCP client will accept: a 101k-char
# response already exceeded the token ceiling and was rejected outright. A
# truncated answer the model can read beats a complete one it never sees, so
# the budget is set below the observed failure point.
MAX_RESPONSE_BYTES = 60_000

# Cap on the number of elements kept in any list nested inside a returned
# dict (voice profiles carry hundreds of term/count pairs, for instance).
MAX_NESTED_LIST_ITEMS = 25


def clamp_limit(limit: Any, default: int = 10) -> int:
    """Coerce a caller-supplied ``limit`` into ``[1, MAX_TOOL_LIMIT]``."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_TOOL_LIMIT))


def bound_text_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Truncate bulky free-text fields in a row dict, in place.

    Adds ``<key>_truncated`` and ``<key>_full_chars`` alongside anything it
    clips, so a shortened value is never mistaken for a complete one.
    """
    for key in TEXT_KEYS:
        v = d.get(key)
        if isinstance(v, str) and len(v) > MAX_HIT_TEXT_CHARS:
            full = len(v)
            d[key] = v[:MAX_HIT_TEXT_CHARS]
            d[f"{key}_truncated"] = True
            d[f"{key}_full_chars"] = full
    return d


def _sizeof(obj: Any) -> int:
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return MAX_HIT_TEXT_CHARS


def bound_response(
    rows: list[dict[str, Any]],
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> list[dict[str, Any]]:
    """Trim a row list so its serialized size stays under ``max_bytes``.

    Always keeps at least one row — a caller is better served by one oversized
    row than by a bare truncation marker. When rows are dropped a trailing
    ``_meta`` entry records how many, so truncation is never silent.
    """
    out: list[dict[str, Any]] = []
    budget = max_bytes
    for i, row in enumerate(rows):
        size = _sizeof(row)
        if budget - size < 0 and out:
            dropped = len(rows) - i
            out.append(
                {
                    "_meta": (
                        f"response truncated to stay under {max_bytes} bytes; "
                        f"{dropped} more row(s) omitted — narrow the query "
                        f"(topic/scope filter) or lower `limit` to see them"
                    ),
                    "truncated": True,
                    "omitted": dropped,
                }
            )
            break
        budget -= size
        out.append(row)
    return out


def bound_mapping(
    payload: dict[str, Any],
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_items: int = MAX_NESTED_LIST_ITEMS,
) -> dict[str, Any]:
    """Shrink a single dict payload (a profile) to fit the budget.

    Profiles are one dict whose *values* are the bulk — hundreds of
    term/count pairs, every measured field with its own provenance map. Three
    passes, cheapest first, stopping as soon as it fits:

    1. truncate long free-text values,
    2. clip nested lists to ``max_items``,
    3. drop the largest remaining keys.

    Anything shortened or dropped is recorded under ``_meta`` so the caller
    can ask for the rest with a narrower tool.
    """
    if not isinstance(payload, dict):
        return payload

    out = dict(payload)
    meta: dict[str, Any] = {}

    bound_text_fields(out)

    # Pass 1: any oversized string, not just the known text keys.
    for key, val in list(out.items()):
        if isinstance(val, str) and len(val) > MAX_HIT_TEXT_CHARS:
            out[key] = val[:MAX_HIT_TEXT_CHARS]
            meta.setdefault("truncated_fields", []).append(key)

    if _sizeof(out) <= max_bytes:
        return out

    # Pass 2: clip nested lists.
    for key, val in list(out.items()):
        if isinstance(val, list) and len(val) > max_items:
            meta.setdefault("clipped_lists", {})[key] = {
                "kept": max_items,
                "total": len(val),
            }
            out[key] = val[:max_items]

    if _sizeof(out) <= max_bytes:
        if meta:
            out["_meta"] = meta
        return out

    # Pass 3: drop the biggest keys until it fits. Sorted by size so the
    # smallest useful fields survive rather than whichever happened to be last.
    sized = sorted(
        ((k, _sizeof(v)) for k, v in out.items() if k != "_meta"),
        key=lambda kv: kv[1],
        reverse=True,
    )
    for key, _size in sized:
        if _sizeof(out) <= max_bytes:
            break
        out.pop(key, None)
        meta.setdefault("dropped_fields", []).append(key)

    if meta:
        meta["reason"] = (
            f"payload exceeded {max_bytes} bytes; use a narrower tool "
            f"(e.g. get_decision_for_topic, check_banned) for full values"
        )
        out["_meta"] = meta
    return out


def bound_keyed_collection(
    items: dict[str, Any],
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    sort_key: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Trim a ``{key: record}`` map to fit ``max_bytes``.

    Returns ``(kept, dropped)``. When ``sort_key`` names a field on the
    records, the most-recent/highest values survive rather than whatever
    order the rows arrived in.
    """
    entries = list(items.items())
    if sort_key:
        entries.sort(key=lambda kv: (kv[1] or {}).get(sort_key) or 0, reverse=True)

    kept: dict[str, Any] = {}
    budget = max_bytes
    for i, (key, val) in enumerate(entries):
        size = _sizeof({key: val})
        if budget - size < 0 and kept:
            return kept, len(entries) - i
        budget -= size
        kept[key] = val
    return kept, 0


__all__ = [
    "MAX_HIT_TEXT_CHARS",
    "MAX_NESTED_LIST_ITEMS",
    "MAX_RESPONSE_BYTES",
    "MAX_TOOL_LIMIT",
    "TEXT_KEYS",
    "bound_keyed_collection",
    "bound_mapping",
    "bound_response",
    "bound_text_fields",
    "clamp_limit",
]
