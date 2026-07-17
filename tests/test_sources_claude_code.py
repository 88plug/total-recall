"""Tests for the Claude Code adapter (:mod:`lib.sources.claude_code`).

Three layers:

1. Hermetic tests built on a synthetic ``projects/`` tree under
   ``tmp_path``. These run anywhere and exercise discovery + record
   streaming via the new ABC surface.
2. A back-compat check that the adapter yields the same record stream
   as the underlying :func:`lib.jsonl_walker.iter_records`.
3. Corpus tests gated on ``~/.claude/projects`` being present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.jsonl_walker import DEFAULT_PROJECTS_ROOT
from lib.jsonl_walker import iter_records as cc_iter_records
from lib.schema import AssistantRecord, PermissionModeRecord, UserRecord
from lib.sources import SOURCES, SessionFile, all_sources, source_by_name
from lib.sources.claude_code import ClaudeCodeSource

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o))
            f.write("\n")


def _make_projects_root(tmp_path: Path) -> Path:
    """Build a tiny ``projects/`` tree with two slugs and three sessions."""
    root = tmp_path / "projects"
    root.mkdir()

    # Project A — two sessions.
    a = root / "-home-operator-foo"
    _write_jsonl(
        a / "11111111-1111-1111-1111-111111111111.jsonl",
        [
            {"type": "permission-mode", "permissionMode": "default"},
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "s1",
                "cwd": "/home/operator/foo",
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )
    _write_jsonl(
        a / "22222222-2222-2222-2222-222222222222.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": None,
                "sessionId": "s2",
                "cwd": "/home/operator/foo",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "ok"}],
                },
            }
        ],
    )

    # Project B — one session.
    b = root / "-home-operator-bar"
    _write_jsonl(
        b / "33333333-3333-3333-3333-333333333333.jsonl",
        [{"type": "permission-mode", "permissionMode": "bypassPermissions"}],
    )

    # A stray non-directory entry — must be ignored.
    (root / "stray.txt").write_text("ignore me")

    return root


# ---------------------------------------------------------------------------
# Identity / availability
# ---------------------------------------------------------------------------


def test_name_constant():
    assert ClaudeCodeSource.name == "claude_code"
    assert ClaudeCodeSource().name == "claude_code"


def test_is_available_true_when_dir_exists(tmp_path: Path):
    root = _make_projects_root(tmp_path)
    assert ClaudeCodeSource(projects_root=root).is_available() is True


def test_is_available_false_when_dir_missing(tmp_path: Path):
    missing = tmp_path / "does" / "not" / "exist"
    assert ClaudeCodeSource(projects_root=missing).is_available() is False


def test_default_projects_root_is_home_dot_claude():
    src = ClaudeCodeSource()
    assert src.projects_root == Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_session_files(tmp_path: Path):
    root = _make_projects_root(tmp_path)
    src = ClaudeCodeSource(projects_root=root)
    sessions = list(src.discover_sessions())

    assert len(sessions) == 3
    assert all(isinstance(s, SessionFile) for s in sessions)
    assert {s.source for s in sessions} == {"claude_code"}

    # Order is deterministic: slug-then-filename, both sorted.
    # -home-operator-bar sorts before -home-operator-foo, so the bar session
    # (33333...) comes first, then the two foo sessions in stem order.
    stems = [s.session_id for s in sessions]
    assert stems == [
        "33333333-3333-3333-3333-333333333333",
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]

    # cwd is derived from slug.
    by_id = {s.session_id: s for s in sessions}
    s1 = by_id["11111111-1111-1111-1111-111111111111"]
    assert s1.cwd == "/home/operator/foo"
    assert s1.extra["slug"] == "-home-operator-foo"
    assert s1.path.is_file()
    assert s1.last_modified > 0
    assert s1.started_at is None  # not eagerly filled


def test_discover_sessions_empty_when_unavailable(tmp_path: Path):
    src = ClaudeCodeSource(projects_root=tmp_path / "nope")
    assert list(src.discover_sessions()) == []


def test_discover_sessions_skips_non_dir_entries(tmp_path: Path):
    root = _make_projects_root(tmp_path)
    src = ClaudeCodeSource(projects_root=root)
    paths = [s.path.name for s in src.discover_sessions()]
    # stray.txt should never appear.
    assert "stray.txt" not in paths


# ---------------------------------------------------------------------------
# Record streaming
# ---------------------------------------------------------------------------


def test_iter_records_round_trips_via_walker(tmp_path: Path):
    root = _make_projects_root(tmp_path)
    src = ClaudeCodeSource(projects_root=root)
    sessions = list(src.discover_sessions())
    target = next(s for s in sessions if s.session_id.startswith("11111111"))

    via_adapter = list(src.iter_records(target))
    via_walker = list(cc_iter_records(target.path))

    # Same length, same offsets, same record uuids/types.
    assert len(via_adapter) == len(via_walker)
    for (oa, ra), (ow, rw) in zip(via_adapter, via_walker, strict=False):
        assert oa == ow
        assert ra.type == rw.type
        assert ra.uuid == rw.uuid


def test_iter_records_emits_expected_record_subclasses(tmp_path: Path):
    root = _make_projects_root(tmp_path)
    src = ClaudeCodeSource(projects_root=root)
    s1 = next(s for s in src.discover_sessions() if s.session_id.startswith("11111111"))
    records = [r for _, r in src.iter_records(s1)]
    assert isinstance(records[0], PermissionModeRecord)
    assert isinstance(records[1], UserRecord)
    assert records[1].text == "hi"

    s2 = next(s for s in src.discover_sessions() if s.session_id.startswith("22222222"))
    records2 = [r for _, r in src.iter_records(s2)]
    assert isinstance(records2[0], AssistantRecord)
    assert records2[0].model == "claude-opus-4-7"


def test_iter_records_respects_start_offset(tmp_path: Path):
    root = _make_projects_root(tmp_path)
    src = ClaudeCodeSource(projects_root=root)
    s1 = next(s for s in src.discover_sessions() if s.session_id.startswith("11111111"))
    full = list(src.iter_records(s1))
    assert len(full) == 2
    mid_offset = full[0][0]
    tail = list(src.iter_records(s1, start_offset=mid_offset))
    assert len(tail) == 1
    assert tail[0][1].uuid == full[1][1].uuid


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_adapter_appears_in_registry():
    assert ClaudeCodeSource in SOURCES


def test_source_by_name_returns_claude_code():
    src = source_by_name("claude_code")
    assert isinstance(src, ClaudeCodeSource)


def test_all_sources_includes_claude_code():
    assert any(isinstance(s, ClaudeCodeSource) for s in all_sources())


# ---------------------------------------------------------------------------
# Corpus (skipped when no real ~/.claude/projects exists)
# ---------------------------------------------------------------------------


def _have_corpus() -> bool:
    return DEFAULT_PROJECTS_ROOT.is_dir()


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects")
def test_corpus_default_adapter_is_available():
    assert ClaudeCodeSource().is_available() is True


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects")
def test_corpus_discover_yields_something():
    src = ClaudeCodeSource()
    seen = 0
    for sf in src.discover_sessions():
        assert sf.source == "claude_code"
        assert sf.path.is_file()
        assert sf.path.suffix == ".jsonl"
        assert sf.session_id == sf.path.stem
        seen += 1
        if seen >= 3:
            break
    assert seen > 0


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects")
def test_corpus_iter_records_streams():
    src = ClaudeCodeSource()
    sf = next(iter(src.discover_sessions()), None)
    if sf is None:
        pytest.skip("no sessions in real corpus")
    # Just confirm the adapter can pull at least one record.
    record_iter = src.iter_records(sf)
    first = next(record_iter, None)
    if first is None:
        pytest.skip("first session is empty")
    off, rec = first
    assert off > 0
    assert rec.type
