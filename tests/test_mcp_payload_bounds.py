"""Regression: MCP responses must stay bounded so a huge indexed row cannot
break the stdio JSON-RPC pipe (which drops total-recall for the whole session).

Seed failure: `search_messages` returned each message's full `text` verbatim.
A single ~172k-char tool-result blob (times `limit` rows) produced a response
large enough to crash the stdio server. These tests pin the fix.
"""

from __future__ import annotations

import json

from mcp_server import tools


def test_clamp_limit_bounds():
    assert tools._clamp_limit(10_000) == tools._MAX_TOOL_LIMIT
    assert tools._clamp_limit(0) == 1
    assert tools._clamp_limit(-5) == 1
    assert tools._clamp_limit(20) == 20
    assert tools._clamp_limit("nonsense") == 10  # default on bad input


def test_row_to_hit_truncates_huge_text():
    huge = "x" * 200_000
    hit = tools._row_to_hit({"id": 1, "role": "assistant", "text": huge})
    assert len(hit["text"]) == tools._MAX_HIT_TEXT_CHARS
    assert hit["text_truncated"] is True
    assert hit["text_full_chars"] == 200_000


def test_row_to_hit_leaves_short_text_untouched():
    hit = tools._row_to_hit({"id": 2, "text": "short answer"})
    assert hit["text"] == "short answer"
    assert "text_truncated" not in hit
    assert "text_full_chars" not in hit


def test_bound_response_caps_total_bytes():
    # Enough capped rows (~1.5 KB each) to blow past the 256 KB budget.
    rows = [
        tools._row_to_hit({"id": i, "text": "y" * 200_000}) for i in range(300)
    ]
    bounded = tools._bound_response(rows)
    size = len(json.dumps(bounded, default=str))
    assert size <= tools._MAX_RESPONSE_BYTES + tools._MAX_HIT_TEXT_CHARS
    # Truncation is never silent.
    assert bounded[-1].get("truncated") is True
    assert bounded[-1]["omitted"] >= 1
    assert len(bounded) < len(rows) + 1


def test_bound_response_passes_small_lists_through():
    rows = [tools._row_to_hit({"id": i, "text": "ok"}) for i in range(3)]
    bounded = tools._bound_response(rows)
    assert len(bounded) == 3
    assert all("truncated" not in r for r in bounded)
