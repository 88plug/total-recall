"""Regression tests for mcp_server.bounds — the shared MCP payload budget.

`tools.py` already bounded its own responses, but none of the 14 modules
under `mcp_server/extras/` used that layer. Once the structured tables were
actually populated the unbounded tools started exceeding the client's token
ceiling, so the caller received an error instead of an answer:

    get_operator_profile     235,725 chars
    list_standing_decisions  101,061 chars

Both are worse than a trimmed answer — an errored tool call returns nothing
usable at all. These tests pin the shared helpers and the budget.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_server import bounds  # noqa: E402


def _size(obj: object) -> int:
    return len(json.dumps(obj, default=str))


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_is_below_the_observed_failure_point() -> None:
    """101,061 chars was rejected by the client; the budget must sit under it."""
    assert bounds.MAX_RESPONSE_BYTES < 101_061


# ---------------------------------------------------------------------------
# clamp_limit
# ---------------------------------------------------------------------------


def test_clamp_limit_bounds() -> None:
    assert bounds.clamp_limit(10_000) == bounds.MAX_TOOL_LIMIT
    assert bounds.clamp_limit(0) == 1
    assert bounds.clamp_limit(-5) == 1
    assert bounds.clamp_limit(20) == 20
    assert bounds.clamp_limit(None) == 10
    assert bounds.clamp_limit("junk", default=25) == 25


# ---------------------------------------------------------------------------
# bound_text_fields
# ---------------------------------------------------------------------------


def test_bound_text_fields_truncates_and_marks() -> None:
    row = {"rationale": "x" * 50_000, "topic": "cloud_provider"}
    out = bounds.bound_text_fields(row)
    assert len(out["rationale"]) == bounds.MAX_HIT_TEXT_CHARS
    assert out["rationale_truncated"] is True
    assert out["rationale_full_chars"] == 50_000
    assert out["topic"] == "cloud_provider"


def test_bound_text_fields_leaves_short_values_alone() -> None:
    out = bounds.bound_text_fields({"reason": "too slow"})
    assert out["reason"] == "too slow"
    assert "reason_truncated" not in out


# ---------------------------------------------------------------------------
# bound_response
# ---------------------------------------------------------------------------


def test_bound_response_caps_total_bytes() -> None:
    rows = [{"id": i, "blob": "y" * 5_000} for i in range(200)]
    out = bounds.bound_response(rows)
    assert _size(out) <= bounds.MAX_RESPONSE_BYTES + 1_000
    assert out[-1]["truncated"] is True
    assert out[-1]["omitted"] > 0


def test_bound_response_never_returns_only_a_marker() -> None:
    """One oversized row still beats a response with no data in it."""
    out = bounds.bound_response([{"id": 1, "blob": "z" * 500_000}])
    assert len(out) == 1
    assert out[0]["id"] == 1


def test_bound_response_passes_small_lists_through() -> None:
    rows = [{"id": i} for i in range(5)]
    assert bounds.bound_response(rows) == rows


# ---------------------------------------------------------------------------
# bound_mapping (profile-shaped payloads)
# ---------------------------------------------------------------------------


def test_bound_mapping_shrinks_a_huge_profile() -> None:
    """Modelled on the real 235,725-char get_operator_profile payload."""
    profile = {
        "name": "Andrew Mello",
        "signature_typos": [[f"term{i}", i] for i in range(5_000)],
        "philosophy": "q" * 80_000,
        "_sources": {f"field{i}": f"session-{i}" for i in range(2_000)},
    }
    assert _size(profile) > 200_000

    out = bounds.bound_mapping(profile)
    assert _size(out) <= bounds.MAX_RESPONSE_BYTES
    assert out["name"] == "Andrew Mello", "cheap identity fields must survive"
    assert "_meta" in out


def test_bound_mapping_records_what_it_clipped() -> None:
    # Each entry ~30 bytes serialized, so 10k entries clears the budget and
    # forces the nested-list pass to run.
    profile = {"name": "x", "items": [{"term": f"t{i}", "count": i} for i in range(10_000)]}
    assert _size(profile) > bounds.MAX_RESPONSE_BYTES
    out = bounds.bound_mapping(profile)
    meta = out.get("_meta", {})
    assert meta.get("clipped_lists", {}).get("items", {}).get("total") == 10_000
    assert len(out["items"]) == bounds.MAX_NESTED_LIST_ITEMS
    assert out["name"] == "x"


def test_bound_mapping_passes_small_payloads_through_unchanged() -> None:
    small = {"name": "Andrew", "role": "operator"}
    assert bounds.bound_mapping(small) == small


def test_bound_mapping_tolerates_non_dict() -> None:
    assert bounds.bound_mapping(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# bound_keyed_collection (project graph)
# ---------------------------------------------------------------------------


def test_bound_keyed_collection_trims_and_counts() -> None:
    graph = {f"/repo/{i}": {"purpose": "p" * 2_000, "last_active_ts": i} for i in range(200)}
    kept, dropped = bounds.bound_keyed_collection(graph, sort_key="last_active_ts")
    assert dropped > 0
    assert len(kept) + dropped == len(graph)
    assert _size(kept) <= bounds.MAX_RESPONSE_BYTES + 5_000


def test_bound_keyed_collection_keeps_most_recent_first() -> None:
    graph = {f"/repo/{i}": {"blob": "b" * 4_000, "last_active_ts": i} for i in range(100)}
    kept, dropped = bounds.bound_keyed_collection(graph, sort_key="last_active_ts")
    assert dropped > 0
    assert "/repo/99" in kept, "highest last_active_ts must survive the trim"
    assert "/repo/0" not in kept


def test_bound_keyed_collection_passes_small_maps_through() -> None:
    graph = {"/repo/a": {"purpose": "x", "last_active_ts": 1}}
    kept, dropped = bounds.bound_keyed_collection(graph)
    assert kept == graph
    assert dropped == 0
