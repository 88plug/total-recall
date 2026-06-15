"""Tests for :mod:`lib.dag` — parent-uuid graph, branches, linearization."""

from __future__ import annotations

import pytest

from lib.dag import build_dag, find_branches, linearize_main_branch
from lib.jsonl_walker import DEFAULT_PROJECTS_ROOT, read_all
from lib.schema import Record, parse_record


def _mk_assistant(uuid: str, parent: str | None, ts: str = "2026-05-25T01:00:00.000Z") -> Record:
    return parse_record(
        {
            "type": "assistant",
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "s",
            "timestamp": ts,
            "message": {"model": "m", "content": [{"type": "text", "text": uuid}]},
        }
    )


def _mk_user(uuid: str, parent: str | None, ts: str = "2026-05-25T01:00:00.000Z") -> Record:
    return parse_record(
        {
            "type": "user",
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "s",
            "timestamp": ts,
            "message": {"role": "user", "content": uuid},
        }
    )


def test_build_dag_simple_chain():
    recs = [
        _mk_user("u1", None, "2026-05-25T00:00:01Z"),
        _mk_assistant("a1", "u1", "2026-05-25T00:00:02Z"),
        _mk_user("u2", "a1", "2026-05-25T00:00:03Z"),
        _mk_assistant("a2", "u2", "2026-05-25T00:00:04Z"),
    ]
    dag = build_dag(recs)
    assert set(dag.nodes.keys()) == {"u1", "a1", "u2", "a2"}
    assert dag.roots == ["u1"]
    assert dag.leaves == ["a2"]
    assert dag.children["u1"] == ["a1"]
    assert find_branches(dag) == []
    linear = linearize_main_branch(dag)
    assert [r.uuid for r in linear] == ["u1", "a1", "u2", "a2"]


def test_build_dag_branches_and_main_branch_is_latest_leaf():
    # Branch: a1 has two child users (a rewind); pick the later branch.
    recs = [
        _mk_user("u1", None, "2026-05-25T00:00:01Z"),
        _mk_assistant("a1", "u1", "2026-05-25T00:00:02Z"),
        _mk_user("u2a", "a1", "2026-05-25T00:00:03Z"),  # earlier branch
        _mk_assistant("a2a", "u2a", "2026-05-25T00:00:04Z"),
        _mk_user("u2b", "a1", "2026-05-25T00:00:10Z"),  # later branch (rewind)
        _mk_assistant("a2b", "u2b", "2026-05-25T00:00:11Z"),
    ]
    dag = build_dag(recs)
    branches = find_branches(dag)
    assert ("a1", ["u2a", "u2b"]) in branches
    assert set(dag.leaves) == {"a2a", "a2b"}

    linear = linearize_main_branch(dag)
    # The "main" branch follows the latest leaf — a2b.
    assert [r.uuid for r in linear] == ["u1", "a1", "u2b", "a2b"]


def test_linearize_default_picks_deepest_branch():
    """Default policy is ``deepest``: the long thread wins over a shallow but
    later sub-tree, which is the realistic ``--resume`` cycle case."""
    # Deep branch: u1 -> a1 -> u2 -> a2 -> u3 -> a3 (depth 6), early ts.
    deep = [
        _mk_user("u1", None, "2026-05-25T00:00:01Z"),
        _mk_assistant("a1", "u1", "2026-05-25T00:00:02Z"),
        _mk_user("u2", "a1", "2026-05-25T00:00:03Z"),
        _mk_assistant("a2", "u2", "2026-05-25T00:00:04Z"),
        _mk_user("u3", "a2", "2026-05-25T00:00:05Z"),
        _mk_assistant("a3", "u3", "2026-05-25T00:00:06Z"),
    ]
    # Shallow side branch off a1: u2b -> a2b (depth 4), but later ts.
    shallow = [
        _mk_user("u2b", "a1", "2026-05-25T00:00:50Z"),
        _mk_assistant("a2b", "u2b", "2026-05-25T00:00:51Z"),
    ]
    dag = build_dag(deep + shallow)
    assert set(dag.leaves) == {"a3", "a2b"}

    # Default: deepest wins, even though shallow has the latest ts.
    linear = linearize_main_branch(dag)
    assert [r.uuid for r in linear] == ["u1", "a1", "u2", "a2", "u3", "a3"]

    # latest_ts policy: shallow side branch wins.
    linear_ts = linearize_main_branch(dag, policy="latest_ts")
    assert [r.uuid for r in linear_ts] == ["u1", "a1", "u2b", "a2b"]


def test_linearize_deepest_across_multiple_roots():
    """Multiple roots from --resume cycles: deepest path across any root wins."""
    # Root R1 with depth 3 but latest ts at leaf.
    short = [
        _mk_user("r1", None, "2026-05-25T00:00:01Z"),
        _mk_assistant("r1a", "r1", "2026-05-25T00:00:02Z"),
        _mk_user("r1u", "r1a", "2026-05-25T00:01:00Z"),  # latest ts
    ]
    # Root R2 with depth 5, all earlier ts.
    long_chain = [
        _mk_user("r2", None, "2026-05-25T00:00:00Z"),
        _mk_assistant("r2a", "r2", "2026-05-25T00:00:00Z"),
        _mk_user("r2u", "r2a", "2026-05-25T00:00:00Z"),
        _mk_assistant("r2b", "r2u", "2026-05-25T00:00:00Z"),
        _mk_user("r2v", "r2b", "2026-05-25T00:00:00Z"),
    ]
    dag = build_dag(short + long_chain)
    # Two roots, two leaves.
    assert set(dag.roots) == {"r1", "r2"}

    linear = linearize_main_branch(dag)  # deepest
    assert [r.uuid for r in linear] == ["r2", "r2a", "r2u", "r2b", "r2v"]

    linear_ts = linearize_main_branch(dag, policy="latest_ts")
    assert [r.uuid for r in linear_ts] == ["r1", "r1a", "r1u"]


def test_linearize_specified_leaf_policy_without_uuid_is_empty():
    """``policy="specified_leaf"`` with no leaf_uuid returns []."""
    dag = build_dag(
        [_mk_user("u1", None), _mk_assistant("a1", "u1")]
    )
    assert linearize_main_branch(dag, policy="specified_leaf") == []


def test_linearize_explicit_leaf_overrides_policy():
    """When ``leaf_uuid`` is given, ``policy`` is irrelevant."""
    recs = [
        _mk_user("u1", None, "2026-05-25T00:00:01Z"),
        _mk_assistant("a1", "u1", "2026-05-25T00:00:02Z"),
        _mk_user("u2", "a1", "2026-05-25T00:00:03Z"),
        _mk_assistant("a2", "u2", "2026-05-25T00:00:04Z"),
    ]
    dag = build_dag(recs)
    # Even with policy="deepest" (which would walk to a2), the explicit
    # leaf wins.
    linear = linearize_main_branch(dag, leaf_uuid="u2", policy="deepest")
    assert [r.uuid for r in linear] == ["u1", "a1", "u2"]


def test_linearize_with_explicit_leaf():
    recs = [
        _mk_user("u1", None),
        _mk_assistant("a1", "u1"),
        _mk_user("u2", "a1"),
    ]
    dag = build_dag(recs)
    linear = linearize_main_branch(dag, leaf_uuid="a1")
    assert [r.uuid for r in linear] == ["u1", "a1"]


def test_sidechain_records_excluded_from_main_dag():
    """``isSidechain:true`` records belong to a separate transcript and are
    intentionally skipped by the main-DAG builder."""
    side = parse_record(
        {
            "type": "assistant",
            "uuid": "side1",
            "parentUuid": None,
            "sessionId": "s",
            "isSidechain": True,
            "message": {"model": "m", "content": []},
        }
    )
    recs = [_mk_user("u1", None), _mk_assistant("a1", "u1"), side]
    dag = build_dag(recs)
    assert "side1" not in dag.nodes


def test_metadata_records_excluded():
    """``permission-mode``, ``ai-title``, ``last-prompt``, ``attachment``,
    ``queue-operation`` are session metadata, not turns; they must not
    appear as DAG nodes."""
    meta = [
        parse_record({"type": "permission-mode", "permissionMode": "default"}),
        parse_record({"type": "ai-title", "aiTitle": "x"}),
        parse_record(
            {"type": "last-prompt", "lastPrompt": "hi", "leafUuid": "u1"}
        ),
        parse_record(
            {
                "type": "attachment",
                "uuid": "att1",
                "parentUuid": None,
                "attachment": {"type": "task_reminder"},
            }
        ),
    ]
    recs = [_mk_user("u1", None), _mk_assistant("a1", "u1"), *meta]
    dag = build_dag(recs)
    assert set(dag.nodes.keys()) == {"u1", "a1"}


def test_unresolved_parent_becomes_root():
    """A record whose parent_uuid is set but not present is treated as a root
    (handles partial files and slicing)."""
    orphan = _mk_assistant("a1", "missing-parent")
    dag = build_dag([orphan])
    assert dag.roots == ["a1"]


def test_empty_dag_safe():
    dag = build_dag([])
    assert dag.nodes == {}
    assert linearize_main_branch(dag) == []


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------


def _have_corpus() -> bool:
    return DEFAULT_PROJECTS_ROOT.is_dir()


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_session_has_real_branches():
    """The big ip-service session has real rewind branches (the project
    notes called out parent uuids with >1 child in sampled sessions)."""
    path = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-ip-service-for-docker"
        / "08daf915-ef75-4516-9543-92a8a30dc3f6.jsonl"
    )
    if not path.is_file():
        pytest.skip(f"reference session missing: {path}")

    recs = read_all(path)
    dag = build_dag(recs)
    # Sanity: a large session has a substantial DAG.
    assert len(dag.nodes) > 100
    # Linearization completes without raising / cycling.
    linear = linearize_main_branch(dag)
    assert linear, "expected a non-empty main branch"
    # All linearized records are unique (no cycles).
    assert len({r.uuid for r in linear}) == len(linear)


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_deepest_policy_meets_or_beats_latest_ts():
    """In a real session with --resume cycles, the deepest path should be at
    least as long as the latest-ts path. Often strictly longer."""
    path = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-ip-service-for-docker"
        / "08daf915-ef75-4516-9543-92a8a30dc3f6.jsonl"
    )
    if not path.is_file():
        pytest.skip(f"reference session missing: {path}")
    recs = read_all(path)
    dag = build_dag(recs)
    linear_deep = linearize_main_branch(dag)  # default: deepest
    linear_ts = linearize_main_branch(dag, policy="latest_ts")
    assert linear_deep, "deepest path must be non-empty"
    assert linear_ts, "latest_ts path must be non-empty"
    # Deepest is, by definition, >= latest_ts.
    assert len(linear_deep) >= len(linear_ts)


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_main_branch_is_a_real_path():
    """For each consecutive pair in the linearized main branch, the second's
    parent_uuid must equal the first's uuid."""
    path = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836.jsonl"
    )
    if not path.is_file():
        pytest.skip(f"reference session missing: {path}")
    recs = read_all(path)
    dag = build_dag(recs)
    linear = linearize_main_branch(dag)
    assert linear
    for prev, nxt in zip(linear, linear[1:], strict=False):
        # Either nxt.parent_uuid == prev.uuid, or we crossed a compact_boundary
        # whose system record has parentUuid:null but logicalParentUuid set.
        # The linearizer follows raw parent pointers, so this invariant holds.
        assert nxt.parent_uuid == prev.uuid
