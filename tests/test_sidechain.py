"""Tests for :mod:`lib.sidechain` — subagent transcript loading and
parent-linkage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.jsonl_walker import DEFAULT_PROJECTS_ROOT, read_all
from lib.schema import UserRecord
from lib.sidechain import (
    link_subagents_to_parent,
    load_subagent,
    load_subagent_meta,
    subagent_files_for_session,
)


def _write(path: Path, objs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o))
            f.write("\n")


def _build_synthetic_session(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Synthesize a parent session + one subagent matching the real layout.

    Returns ``(session_jsonl, subagent_dir, agent_id, tool_use_id)``.
    """

    session_uuid = "11111111-1111-1111-1111-111111111111"
    agent_id = "aBC123def456"
    tool_use_id = "toolu_synth_001"

    session_jsonl = tmp_path / "-home-operator-foo" / f"{session_uuid}.jsonl"
    parent_records = [
        # Parent assistant turn with a TaskCreate tool_use
        {
            "type": "assistant",
            "uuid": "p_a1",
            "parentUuid": None,
            "sessionId": session_uuid,
            "message": {
                "model": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "TaskCreate",
                        "input": {"description": "do a thing"},
                    }
                ],
            },
        },
        # Parent user turn carrying the completion tool_result + toolUseResult.agentId
        {
            "type": "user",
            "uuid": "p_u1",
            "parentUuid": "p_a1",
            "sessionId": session_uuid,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": False,
                        "content": "subagent done",
                    }
                ],
            },
            "toolUseResult": {
                "agentId": agent_id,
                "agentType": "general-purpose",
                "status": "completed",
                "content": "subagent done",
                "totalDurationMs": 1234,
            },
        },
    ]
    _write(session_jsonl, parent_records)

    sub_dir = session_jsonl.with_suffix("") / "subagents"
    sub_jsonl = sub_dir / f"agent-{agent_id}.jsonl"
    sub_records = [
        {
            "type": "user",
            "uuid": "s_u1",
            "parentUuid": None,
            "isSidechain": True,
            "agentId": agent_id,
            "sessionId": session_uuid,
            "message": {"role": "user", "content": "go research"},
        },
        {
            "type": "assistant",
            "uuid": "s_a1",
            "parentUuid": "s_u1",
            "isSidechain": True,
            "sessionId": session_uuid,
            "message": {"model": "m", "content": [{"type": "text", "text": "done"}]},
        },
    ]
    _write(sub_jsonl, sub_records)
    meta_path = sub_dir / f"agent-{agent_id}.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "do a thing",
                "toolUseId": tool_use_id,
            }
        ),
        encoding="utf-8",
    )

    return session_jsonl, sub_dir, agent_id, tool_use_id


def test_subagent_files_for_session(tmp_path: Path):
    session_jsonl, sub_dir, agent_id, _ = _build_synthetic_session(tmp_path)
    pairs = subagent_files_for_session(session_jsonl)
    assert len(pairs) == 1
    found_id, found_path = pairs[0]
    assert found_id == agent_id
    assert found_path.parent == sub_dir


def test_subagent_files_missing_dir_returns_empty(tmp_path: Path):
    p = tmp_path / "x.jsonl"
    p.write_text("", encoding="utf-8")
    assert subagent_files_for_session(p) == []


def test_load_subagent_and_meta(tmp_path: Path):
    session_jsonl, _, agent_id, tool_use_id = _build_synthetic_session(tmp_path)
    pairs = subagent_files_for_session(session_jsonl)
    assert pairs
    _, agent_path = pairs[0]

    records = load_subagent(agent_path)
    assert len(records) == 2
    assert all(r.is_sidechain for r in records)
    assert records[0].parent_uuid is None  # first sidechain record has null parent

    meta = load_subagent_meta(agent_path)
    assert meta["toolUseId"] == tool_use_id
    assert meta["agentType"] == "general-purpose"


def test_link_subagents_keyed_by_parent_tool_use_id(tmp_path: Path):
    session_jsonl, sub_dir, _, tool_use_id = _build_synthetic_session(tmp_path)
    parent_records = read_all(session_jsonl)

    linked = link_subagents_to_parent(parent_records, sub_dir)
    # Keyed by the parent TaskCreate's tool_use id.
    assert tool_use_id in linked
    sub_records = linked[tool_use_id]
    assert len(sub_records) == 2
    assert sub_records[0].uuid == "s_u1"
    assert sub_records[1].uuid == "s_a1"


def test_link_subagents_falls_back_to_meta(tmp_path: Path):
    """If the parent has no completion record yet, the linker still keys by
    the meta-file's toolUseId so the subagent records aren't lost."""
    session_jsonl, sub_dir, _, tool_use_id = _build_synthetic_session(tmp_path)
    # Pass an empty parent-records list to simulate the "subagent still running" case.
    linked = link_subagents_to_parent([], sub_dir)
    assert tool_use_id in linked
    assert len(linked[tool_use_id]) == 2


def test_link_subagents_unmatched_uses_agent_prefix(tmp_path: Path):
    """If neither parent nor meta provides a toolUseId, key by ``agent:<id>``."""
    session_jsonl, sub_dir, agent_id, _ = _build_synthetic_session(tmp_path)
    # Nuke the meta file's toolUseId so the fallback kicks in.
    meta_path = sub_dir / f"agent-{agent_id}.meta.json"
    meta_path.write_text(json.dumps({"agentType": "x"}), encoding="utf-8")

    linked = link_subagents_to_parent([], sub_dir)
    assert f"agent:{agent_id}" in linked


def test_link_subagents_ignores_non_agent_files(tmp_path: Path):
    """Random files under ``subagents/`` should be skipped."""
    session_jsonl, sub_dir, _, _ = _build_synthetic_session(tmp_path)
    (sub_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    parent_records = read_all(session_jsonl)
    linked = link_subagents_to_parent(parent_records, sub_dir)
    # Still exactly the one real subagent.
    assert sum(len(v) for v in linked.values()) == 2


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------


def _have_corpus() -> bool:
    return DEFAULT_PROJECTS_ROOT.is_dir()


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_amnesia_session_has_subagents():
    """The reference amnesia session uses TaskCreate; the on-disk
    sidechain dir should hold agent-*.jsonl pairs."""
    session_jsonl = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836.jsonl"
    )
    sub_dir = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836"
        / "subagents"
    )
    if not session_jsonl.is_file() or not sub_dir.is_dir():
        pytest.skip("reference session/subagents missing")

    pairs = subagent_files_for_session(session_jsonl)
    assert len(pairs) >= 1

    # Pick the first subagent, verify load + sidechain marker.
    _, agent_path = pairs[0]
    records = load_subagent(agent_path)
    assert records
    assert records[0].is_sidechain is True
    assert records[0].parent_uuid is None

    # Linker keys at least one entry by some non-empty key.
    parent_records = read_all(session_jsonl)
    linked = link_subagents_to_parent(parent_records, sub_dir)
    assert linked
    assert all(isinstance(k, str) and k for k in linked)


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_link_keys_match_real_tool_use_ids():
    """At least one subagent should link to a real parent tool_use_id (not
    fall back to ``agent:<id>`` / meta-only)."""
    session_jsonl = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836.jsonl"
    )
    sub_dir = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836"
        / "subagents"
    )
    if not session_jsonl.is_file() or not sub_dir.is_dir():
        pytest.skip("reference session/subagents missing")

    parent_records = read_all(session_jsonl)
    linked = link_subagents_to_parent(parent_records, sub_dir)

    # Collect all parent tool_use_ids from the user.tool_results envelopes.
    real_tool_use_ids = set()
    for r in parent_records:
        if isinstance(r, UserRecord) and r.tool_results:
            for tr in r.tool_results:
                if tr.tool_use_id:
                    real_tool_use_ids.add(tr.tool_use_id)

    # At least one linked key should be a real parent tool_use_id.
    overlap = real_tool_use_ids & set(linked.keys())
    assert overlap, (
        f"no subagent keyed to a real parent tool_use_id; keys={list(linked.keys())[:5]}"
    )
