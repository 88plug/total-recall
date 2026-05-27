"""Tests for ``hooks.lib.session_state``.

The state file is the durability layer for the signal-driven re-injection
pipeline. These tests pin:

* the on-disk path layout (other hooks shell-call into it),
* the schema returned by ``load_state`` for a missing file,
* the atomic-rename behaviour of ``save_state``,
* the bookkeeping invariants of ``advance_turn`` and ``record_inject``,
* graceful degradation on corrupt JSON.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

# Make ``hooks.lib`` importable regardless of how pytest is invoked.
_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hooks.lib import session_state as ss  # noqa: E402


# ---------------------------------------------------------------------------
# fixture: isolated CLAUDE_PLUGIN_DATA per-test
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_data(tmp_path, monkeypatch):
    """Point ``CLAUDE_PLUGIN_DATA`` at a throwaway dir for the test."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# state_path
# ---------------------------------------------------------------------------


def test_state_path_layout(plugin_data):
    p = ss.state_path("abc-123")
    assert p == plugin_data / "total-recall" / "sessions" / "abc-123.json"
    # Parent dir created with mode 0o700 (mask out the upper bits).
    assert p.parent.is_dir()
    mode = p.parent.stat().st_mode & 0o777
    assert mode == 0o700


def test_state_path_creates_parents(plugin_data):
    # Even nested under a non-existent root, the function bootstraps.
    nested = plugin_data / "deeper" / "still"
    os.environ["CLAUDE_PLUGIN_DATA"] = str(nested)
    p = ss.state_path("s1")
    assert p.parent.exists()


def test_state_path_falls_back_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setattr(
        os.path, "expanduser", lambda x: str(tmp_path) if "total-recall" in x else x
    )
    p = ss.state_path("s1")
    # The fallback expands ``~/.local/share/total-recall`` — assert the
    # filename is correct regardless of where it resolved to.
    assert p.name == "s1.json"


# ---------------------------------------------------------------------------
# load_state — defaults + corruption tolerance
# ---------------------------------------------------------------------------


def test_load_state_missing_returns_default(plugin_data):
    state = ss.load_state("new-session")
    assert state["session_id"] == "new-session"
    assert state["cwd"] == ""
    assert state["prev_cwd"] is None
    assert state["turn"] == 0
    assert state["last_inject_ts"] == 0.0
    assert state["turns_since_inject"] == 0
    assert state["injections_count"] == 0
    assert state["post_compact"] is False
    assert state["recent_prompts"] == []
    assert state["last_assistant_was_tool_heavy"] is False
    assert state["same_topic_streak"] == 0
    assert state["last_scope"] is None


def test_load_state_default_schema_keys(plugin_data):
    """Pin the full schema so downstream readers don't trip on missing keys."""
    state = ss.load_state("x")
    expected = {
        "session_id",
        "cwd",
        "prev_cwd",
        "turn",
        "last_inject_ts",
        "turns_since_inject",
        "injections_count",
        "post_compact",
        "recent_prompts",
        "last_assistant_was_tool_heavy",
        "same_topic_streak",
        "last_scope",
    }
    assert set(state.keys()) == expected


def test_load_state_corrupt_file_returns_default(plugin_data):
    p = ss.state_path("corrupt")
    p.write_text("{ this is not json")
    state = ss.load_state("corrupt")
    # Falls back cleanly to defaults rather than raising.
    assert state["session_id"] == "corrupt"
    assert state["turn"] == 0


def test_load_state_empty_file_returns_default(plugin_data):
    p = ss.state_path("empty")
    p.write_text("")
    state = ss.load_state("empty")
    assert state["session_id"] == "empty"


# ---------------------------------------------------------------------------
# save_state — atomicity + round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrip(plugin_data):
    s = ss.load_state("rt")
    s["cwd"] = "/home/operator/ip-service-for-docker"
    s["turn"] = 5
    s["recent_prompts"] = ["a", "b", "c"]
    s["last_scope"] = "acme-net"
    ss.save_state(s)
    loaded = ss.load_state("rt")
    assert loaded["cwd"] == "/home/operator/ip-service-for-docker"
    assert loaded["turn"] == 5
    assert loaded["recent_prompts"] == ["a", "b", "c"]
    assert loaded["last_scope"] == "acme-net"


def test_save_state_uses_atomic_rename(plugin_data):
    s = ss.load_state("atomic")
    ss.save_state(s)
    p = ss.state_path("atomic")
    assert p.exists()
    # No leftover .tmp from a successful write.
    assert not p.with_suffix(".json.tmp").exists()


def test_save_state_overwrites_existing(plugin_data):
    s = ss.load_state("ow")
    s["turn"] = 1
    ss.save_state(s)
    s["turn"] = 99
    ss.save_state(s)
    assert ss.load_state("ow")["turn"] == 99


def test_save_state_serialises_non_json_natively(plugin_data):
    """``default=str`` keeps the file writable even with odd values."""

    class Weird:
        def __str__(self) -> str:
            return "weird"

    s = ss.load_state("weird")
    s["last_scope"] = Weird()
    ss.save_state(s)
    raw = json.loads(ss.state_path("weird").read_text())
    assert raw["last_scope"] == "weird"


# ---------------------------------------------------------------------------
# record_inject
# ---------------------------------------------------------------------------


def test_record_inject_updates_counters(plugin_data):
    s = ss.load_state("ri")
    s["turns_since_inject"] = 12
    s["injections_count"] = 2
    s["post_compact"] = True
    before = time.time()
    ss.record_inject(s)
    after = time.time()
    assert s["turns_since_inject"] == 0
    assert s["injections_count"] == 3
    assert before <= s["last_inject_ts"] <= after
    # post_compact is one-shot — consumed on inject.
    assert s["post_compact"] is False


def test_record_inject_initialises_injections_count(plugin_data):
    s = {"session_id": "ic"}
    ss.record_inject(s)
    assert s["injections_count"] == 1


# ---------------------------------------------------------------------------
# advance_turn
# ---------------------------------------------------------------------------


def test_advance_turn_rolls_prev_cwd(plugin_data):
    s = ss.load_state("at")
    s["cwd"] = "/home/operator/project-a"
    ss.advance_turn(s, "/home/operator/project-b", "switch context")
    assert s["prev_cwd"] == "/home/operator/project-a"
    assert s["cwd"] == "/home/operator/project-b"


def test_advance_turn_bumps_counters(plugin_data):
    s = ss.load_state("c")
    ss.advance_turn(s, "/a", "p1")
    assert s["turn"] == 1
    assert s["turns_since_inject"] == 1
    ss.advance_turn(s, "/a", "p2")
    assert s["turn"] == 2
    assert s["turns_since_inject"] == 2


def test_advance_turn_window_is_last_five(plugin_data):
    s = ss.load_state("w")
    for i in range(8):
        ss.advance_turn(s, "/a", f"p{i}")
    assert s["recent_prompts"] == ["p3", "p4", "p5", "p6", "p7"]
    assert len(s["recent_prompts"]) == 5


def test_advance_turn_initial_prev_cwd_is_empty_string(plugin_data):
    """First turn ever: ``prev_cwd`` rolls from the default ``""``."""
    s = ss.load_state("first")
    ss.advance_turn(s, "/home/operator/x", "first prompt")
    # Default cwd is "" — that's what gets rolled into prev_cwd.
    assert s["prev_cwd"] == ""
    assert s["cwd"] == "/home/operator/x"
    assert s["turn"] == 1


def test_advance_turn_persists_across_save(plugin_data):
    s = ss.load_state("p")
    ss.advance_turn(s, "/a", "hello there friends")
    ss.save_state(s)
    loaded = ss.load_state("p")
    assert loaded["turn"] == 1
    assert loaded["recent_prompts"] == ["hello there friends"]
