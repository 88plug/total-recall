"""Tests for the bidirectional satisfaction pipeline.

Covers:
* Drift trigger in :mod:`detector.escalation` (fires +2, existing tests unaffected).
* Reaction classification in :mod:`extractors.satisfaction`.
* AI-behavior classification in :mod:`extractors.satisfaction`.
* End-to-end extraction from synthetic DAGs.
* Persistence and retrieval via :mod:`index.satisfaction`.
"""

from __future__ import annotations

import sqlite3
import time

from detector.escalation import assess_escalation
from extractors.satisfaction import (
    classify_ai_behavior,
    classify_reaction,
    extract_satisfaction_incremental,
)
from index.satisfaction import (
    ensure_schema,
    get_satisfaction_summary,
    persist_satisfaction,
    upsert_satisfaction_pair,
)

# ---------------------------------------------------------------------------
# Helper: in-memory SQLite connection
# ---------------------------------------------------------------------------


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# PART 1 — Drift trigger in escalation.py
# ---------------------------------------------------------------------------


def test_drift_trigger_fires_on_youre_drifting():
    """'you're drifting' must add exactly +2 and appear in triggers."""
    r = assess_escalation("you're drifting")
    assert "drift" in r.triggers
    assert r.risk == 2


def test_drift_trigger_fires_on_off_track():
    r = assess_escalation("you're off track again")
    assert "drift" in r.triggers
    assert r.risk >= 2


def test_drift_trigger_fires_on_diverging():
    r = assess_escalation("we're diverging from the plan")
    assert "drift" in r.triggers


def test_drift_trigger_weight_is_additive():
    """Drift +2 stacks on top of directive_flip +1 without changing other weights."""
    r = assess_escalation("wait you're drifting from the goal")
    assert "directive_flip" in r.triggers
    assert "drift" in r.triggers
    assert r.risk == 3


def test_drift_does_not_fire_on_unrelated_text():
    r = assess_escalation("looks good to me")
    assert "drift" not in r.triggers


# ---------------------------------------------------------------------------
# PART 2 — Reaction classification
# ---------------------------------------------------------------------------


def test_classify_reaction_praise_quality():
    assert classify_reaction("perfect") == "praise_quality"
    assert classify_reaction("exactly what I wanted") == "praise_quality"
    assert classify_reaction("great, thanks") == "praise_quality"


def test_classify_reaction_praise_launch():
    assert classify_reaction("yes") == "praise_launch"
    assert classify_reaction("go") == "praise_launch"
    assert classify_reaction("ship it") == "praise_launch"
    assert classify_reaction("ok") == "praise_launch"


def test_classify_reaction_frustration_drift():
    assert classify_reaction("you're drifting") == "frustration_drift"
    assert classify_reaction("fix the drift") == "frustration_drift"
    assert classify_reaction("off track") == "frustration_drift"


def test_classify_reaction_frustration_broke():
    assert classify_reaction("you broke it") == "frustration_broke"
    assert classify_reaction("broke css") == "frustration_broke"


def test_classify_reaction_frustration_wtf():
    assert classify_reaction("wtf is this") == "frustration_wtf"
    assert classify_reaction("fuck that") == "frustration_wtf"


def test_classify_reaction_frustration_scope():
    assert classify_reaction("I didn't ask for that") == "frustration_scope"
    assert classify_reaction("too much, stick to the task") == "frustration_scope"


def test_classify_reaction_frustration_verbosity():
    assert classify_reaction("too long") == "frustration_verbosity"
    assert classify_reaction("shorter please") == "frustration_verbosity"
    assert classify_reaction("tl;dr") == "frustration_verbosity"


def test_classify_reaction_silent_accept():
    """≤2-word turns that are not praise or frustration → silent_accept."""
    assert classify_reaction("cool") == "silent_accept"
    assert classify_reaction("noted") == "silent_accept"
    assert classify_reaction("sure thing") == "silent_accept"


def test_classify_reaction_none_on_long_neutral():
    """Longer turns with no signal should return None."""
    assert classify_reaction("I think this is an interesting approach") is None


def test_classify_reaction_frustration_beats_silent_accept():
    """A two-word frustration phrase must NOT fall through to silent_accept."""
    assert classify_reaction("wtf dude") == "frustration_wtf"


# ---------------------------------------------------------------------------
# PART 3 — AI-behavior classification
# ---------------------------------------------------------------------------


def _tool_blocks(text: str = "") -> list[dict]:
    blocks: list[dict] = [{"type": "tool_use", "name": "bash", "input": {}}]
    if text:
        blocks.insert(0, {"type": "text", "text": text})
    return blocks


def test_classify_ai_tool_call_brief():
    blocks = _tool_blocks("Running…")
    assert classify_ai_behavior(blocks, "Running…") == "tool_call_brief"


def test_classify_ai_tool_call_verbose():
    long_text = "x" * 250
    blocks = _tool_blocks(long_text)
    assert classify_ai_behavior(blocks, long_text) == "tool_call_verbose"


def test_classify_ai_long_prose():
    text = "w " * 200  # well over 400 chars
    blocks = [{"type": "text", "text": text}]
    assert classify_ai_behavior(blocks, text) == "long_prose"


def test_classify_ai_mid_prose():
    text = "w " * 60  # ~120 chars
    blocks = [{"type": "text", "text": text}]
    assert classify_ai_behavior(blocks, text) == "mid_prose"


def test_classify_ai_short_ack():
    text = "Done."
    blocks = [{"type": "text", "text": text}]
    assert classify_ai_behavior(blocks, text) == "short_ack"


def test_classify_ai_confirmation_request():
    text = "w " * 60 + "Should I proceed?"
    blocks = [{"type": "text", "text": text}]
    assert classify_ai_behavior(blocks, text) == "confirmation_request"


# ---------------------------------------------------------------------------
# PART 4 — End-to-end extraction from synthetic DAGs
# ---------------------------------------------------------------------------


def _make_record(uuid, parent_uuid, rec_type, content, ts=None):
    """Build a minimal session record dict."""
    record = {
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "type": rec_type,
        "timestamp": ts or int(time.time()),
    }
    if rec_type == "assistant":
        record["message"] = {"role": "assistant", "content": content}
    else:
        if isinstance(content, str):
            record["message"] = {"role": "user", "content": content}
        else:
            record["message"] = {"role": "user", "content": content}
    return record


def _make_assistant(uuid, parent_uuid, blocks, ts=None):
    return _make_record(uuid, parent_uuid, "assistant", blocks, ts)


def _make_user(uuid, parent_uuid, text, ts=None):
    return _make_record(uuid, parent_uuid, "user", text, ts)


def test_extraction_praise_quality_tool_call_brief():
    """assistant tool_call_brief → user 'perfect' → (praise_quality, tool_call_brief) = 1."""
    blocks = [
        {"type": "tool_use", "name": "bash", "input": {}},
        {"type": "text", "text": "ok"},
    ]
    records = [
        _make_assistant("a1", None, blocks),
        _make_user("u1", "a1", "perfect"),
    ]
    profile = extract_satisfaction_incremental(records, {})
    assert profile["matrix"].get("praise_quality", {}).get("tool_call_brief", 0) == 1


def test_extraction_frustration_verbosity_long_prose():
    """assistant long_prose → user 'too long' → (frustration_verbosity, long_prose) = 1."""
    long_text = "word " * 100
    blocks = [{"type": "text", "text": long_text}]
    records = [
        _make_assistant("a1", None, blocks),
        _make_user("u1", "a1", "too long"),
    ]
    profile = extract_satisfaction_incremental(records, {})
    assert profile["matrix"].get("frustration_verbosity", {}).get("long_prose", 0) == 1


def test_extraction_silent_accept():
    """≤2-word non-praise non-frustration turn → silent_accept."""
    blocks = [{"type": "text", "text": "word " * 80}]
    records = [
        _make_assistant("a1", None, blocks),
        _make_user("u1", "a1", "sure"),
    ]
    profile = extract_satisfaction_incremental(records, {})
    assert profile["matrix"].get("silent_accept", {})


def test_extraction_accumulates_across_merges():
    """extract_satisfaction_incremental accumulates counts when called twice."""
    blocks = [{"type": "tool_use", "name": "bash", "input": {}}]
    records = [
        _make_assistant("a1", None, blocks),
        _make_user("u1", "a1", "perfect"),
    ]
    profile1 = extract_satisfaction_incremental(records, {})
    profile2 = extract_satisfaction_incremental(records, profile1)
    assert profile2["matrix"]["praise_quality"]["tool_call_brief"] == 2


def test_extraction_no_reaction_on_long_neutral_turn():
    """A long neutral user turn should not generate any reaction entry."""
    blocks = [{"type": "text", "text": "word " * 10}]
    records = [
        _make_assistant("a1", None, blocks),
        _make_user("u1", "a1", "I think this approach is interesting but let me check"),
    ]
    profile = extract_satisfaction_incremental(records, {})
    assert profile["sample_size"] == 0


# ---------------------------------------------------------------------------
# PART 5 — Persistence and retrieval
# ---------------------------------------------------------------------------


def test_upsert_and_get_summary():
    conn = _mem_conn()
    upsert_satisfaction_pair(conn, "praise_quality", "tool_call_brief", 3)
    upsert_satisfaction_pair(conn, "frustration_verbosity", "long_prose", 2)
    conn.commit()

    summary = get_satisfaction_summary(conn)
    assert summary["matrix"]["praise_quality"]["tool_call_brief"] == 3
    assert summary["matrix"]["frustration_verbosity"]["long_prose"] == 2
    assert summary["top_praise_behavior"] == "tool_call_brief"
    assert summary["top_frustration_behavior"] == "long_prose"


def test_upsert_accumulates():
    conn = _mem_conn()
    upsert_satisfaction_pair(conn, "praise_quality", "short_ack", 1)
    upsert_satisfaction_pair(conn, "praise_quality", "short_ack", 4)
    conn.commit()
    summary = get_satisfaction_summary(conn)
    assert summary["matrix"]["praise_quality"]["short_ack"] == 5


def test_persist_satisfaction_roundtrip():
    """persist_satisfaction → get_satisfaction_summary roundtrip."""
    conn = _mem_conn()
    profile = {
        "matrix": {
            "praise_quality": {"tool_call_brief": 5},
            "frustration_drift": {"long_prose": 3},
        },
        "sample_size": 8,
        "total_praise_count": 5,
        "total_frustration_count": 3,
    }
    persist_satisfaction(conn, profile)
    conn.commit()

    summary = get_satisfaction_summary(conn)
    assert summary["matrix"]["praise_quality"]["tool_call_brief"] == 5
    assert summary["matrix"]["frustration_drift"]["long_prose"] == 3
    assert summary["top_praise_behavior"] == "tool_call_brief"
    assert summary["top_frustration_behavior"] == "long_prose"
    assert summary["sample_size"] == 8


def test_get_summary_empty_db():
    """Empty DB returns zero sample_size and None top picks without error."""
    conn = _mem_conn()
    summary = get_satisfaction_summary(conn)
    assert summary["sample_size"] == 0
    assert summary["top_praise_behavior"] is None
    assert summary["top_frustration_behavior"] is None
    assert summary["matrix"] == {}
