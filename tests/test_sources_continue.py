"""Tests for the Continue (continue.dev) adapter.

All tests are hermetic: they build a synthetic ``sessions/`` tree under
``tmp_path`` and never touch ``~/.continue``. The on-disk corpus check
at the bottom is opt-in via the real default path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.schema import AssistantRecord, SystemRecord, UserRecord
from lib.sources import SOURCES, SessionFile, all_sources, source_by_name
from lib.sources.continue_dev import ContinueSource


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_session(
    dir_: Path,
    session_id: str,
    *,
    workspace: str,
    title: str,
    history: list[dict],
    chat_model_title: str | None = "claude-3-7-sonnet-latest",
    usage: dict | None = None,
) -> Path:
    body: dict = {
        "sessionId": session_id,
        "title": title,
        "workspaceDirectory": workspace,
        "history": history,
    }
    if chat_model_title is not None:
        body["chatModelTitle"] = chat_model_title
    if usage is not None:
        body["usage"] = usage
    path = dir_ / f"{session_id}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _write_index(dir_: Path, entries: list[dict]) -> Path:
    path = dir_ / "sessions.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _make_continue_tree(tmp_path: Path) -> Path:
    sessions_dir = tmp_path / "continue" / "sessions"
    sessions_dir.mkdir(parents=True)

    history_a = [
        {"message": {"role": "user", "content": "build me a thing"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "sure"},
                    {
                        "type": "tool_use",
                        "id": "tc-1",
                        "name": "edit_file",
                        "input": {"path": "/tmp/x", "patch": "..."},
                    },
                ],
            },
            "toolCallStates": [
                {
                    "toolCallId": "tc-1",
                    "toolCall": {
                        "function": {"name": "edit_file", "arguments": "{\"path\":\"/tmp/x\"}"},
                    },
                }
            ],
        },
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tc-1",
                        "is_error": False,
                        "content": "applied",
                    }
                ],
            }
        },
        {"message": {"role": "system", "content": "session restarted"}},
    ]
    _write_session(
        sessions_dir,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        workspace="/home/u/proj-a",
        title="task A",
        history=history_a,
        usage={"inputTokens": 10, "outputTokens": 20},
    )

    history_b = [
        {"message": {"role": "user", "content": "hello"}},
        {
            "message": {
                "role": "assistant",
                "content": "hi back",  # bare string content
            }
        },
    ]
    _write_session(
        sessions_dir,
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        workspace="/home/u/proj-b",
        title="task B",
        history=history_b,
        chat_model_title=None,
    )

    _write_index(
        sessions_dir,
        [
            {
                "sessionId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "title": "task A",
                "workspaceDirectory": "/home/u/proj-a",
                "dateCreated": 1_700_000_000_000,
            },
            {
                "sessionId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "title": "task B",
                "workspaceDirectory": "/home/u/proj-b",
                "dateCreated": 1_700_001_000_000,
            },
        ],
    )

    return sessions_dir


# ---------------------------------------------------------------------------
# Identity / availability
# ---------------------------------------------------------------------------


def test_name_constant():
    assert ContinueSource.name == "continue"
    assert ContinueSource().name == "continue"


def test_default_sessions_dir(monkeypatch):
    monkeypatch.delenv("CONTINUE_GLOBAL_DIR", raising=False)
    src = ContinueSource()
    assert src.sessions_dir == Path.home() / ".continue" / "sessions"


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(tmp_path / "alt"))
    src = ContinueSource(sessions_dir=tmp_path / "ignored")
    assert src.sessions_dir == tmp_path / "alt" / "sessions"


def test_is_available_false_when_missing(tmp_path):
    assert ContinueSource(sessions_dir=tmp_path / "nope").is_available() is False


def test_is_available_true_with_index_only(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "sessions.json").write_text("[]")
    assert ContinueSource(sessions_dir=d).is_available() is True


def test_is_available_true_with_session_only(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "abc.json").write_text("{}")
    assert ContinueSource(sessions_dir=d).is_available() is True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_session_files(tmp_path):
    d = _make_continue_tree(tmp_path)
    src = ContinueSource(sessions_dir=d)
    sessions = list(src.discover_sessions())

    assert len(sessions) == 2
    assert all(isinstance(s, SessionFile) for s in sessions)
    assert {s.source for s in sessions} == {"continue"}

    ids = [s.session_id for s in sessions]
    assert ids == sorted(ids)
    by_id = {s.session_id: s for s in sessions}
    a = by_id["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    assert a.cwd == "/home/u/proj-a"
    assert a.extra.get("title") == "task A"
    assert a.started_at == pytest.approx(1_700_000_000.0)


def test_discover_skips_index_file(tmp_path):
    d = _make_continue_tree(tmp_path)
    src = ContinueSource(sessions_dir=d)
    paths = [s.path.name for s in src.discover_sessions()]
    assert "sessions.json" not in paths


def test_discover_handles_session_without_index_entry(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    _write_session(
        d,
        "no-index-id",
        workspace="/x",
        title="t",
        history=[{"message": {"role": "user", "content": "hi"}}],
    )
    # No sessions.json at all — discovery should still find the file.
    sessions = list(ContinueSource(sessions_dir=d).discover_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "no-index-id"
    assert sessions[0].extra == {}


def test_discover_empty_when_unavailable(tmp_path):
    src = ContinueSource(sessions_dir=tmp_path / "nope")
    assert list(src.discover_sessions()) == []


# ---------------------------------------------------------------------------
# Record streaming
# ---------------------------------------------------------------------------


def test_iter_records_projects_chathistory_items(tmp_path):
    d = _make_continue_tree(tmp_path)
    src = ContinueSource(sessions_dir=d)
    a = next(s for s in src.discover_sessions() if s.session_id.startswith("aaaa"))
    records = [r for _, r in src.iter_records(a)]
    assert len(records) == 4

    # 0: user prompt.
    assert isinstance(records[0], UserRecord)
    assert records[0].text == "build me a thing"
    assert records[0].content_kind == "string"

    # 1: assistant w/ text + tool_use block.
    assert isinstance(records[1], AssistantRecord)
    assert records[1].model == "claude-3-7-sonnet-latest"
    assert records[1].usage == {"inputTokens": 10, "outputTokens": 20}
    types = [b.type for b in records[1].content]
    assert "text" in types and "tool_use" in types
    tu = next(b.tool_use for b in records[1].content if b.type == "tool_use")
    assert tu.id == "tc-1"
    assert tu.name == "edit_file"
    assert tu.input == {"path": "/tmp/x", "patch": "..."}

    # 2: user tool_result.
    assert isinstance(records[2], UserRecord)
    assert records[2].content_kind == "tool_result"
    assert len(records[2].tool_results) == 1
    tr = records[2].tool_results[0]
    assert tr.tool_use_id == "tc-1"
    assert tr.content == "applied"

    # 3: system.
    assert isinstance(records[3], SystemRecord)
    assert records[3].subtype == "continue_system"


def test_iter_records_attaches_usage_only_to_first_assistant(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    _write_session(
        d,
        "multi",
        workspace="/w",
        title="t",
        history=[
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "1"}]}},
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "2"}]}},
        ],
        usage={"inputTokens": 1},
    )
    src = ContinueSource(sessions_dir=d)
    sf = next(iter(src.discover_sessions()))
    recs = [r for _, r in src.iter_records(sf)]
    assert recs[0].usage == {"inputTokens": 1}
    assert recs[1].usage is None


def test_iter_records_tool_use_falls_back_to_toolcallstates(tmp_path):
    """When the assistant message content lacks tool_use blocks, we should
    still surface tool calls from ``toolCallStates``."""

    d = tmp_path / "sessions"
    d.mkdir()
    _write_session(
        d,
        "tcs",
        workspace="/w",
        title="t",
        history=[
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "calling"}],
                },
                "toolCallStates": [
                    {
                        "toolCallId": "id-x",
                        "toolCall": {
                            "function": {
                                "name": "do_thing",
                                "arguments": "{\"a\": 1}",
                            }
                        },
                    }
                ],
            }
        ],
    )
    src = ContinueSource(sessions_dir=d)
    sf = next(iter(src.discover_sessions()))
    rec = list(src.iter_records(sf))[0][1]
    assert isinstance(rec, AssistantRecord)
    tool_blocks = [b for b in rec.content if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].tool_use.id == "id-x"
    assert tool_blocks[0].tool_use.name == "do_thing"
    assert tool_blocks[0].tool_use.input == {"a": 1}


def test_iter_records_respects_start_offset(tmp_path):
    d = _make_continue_tree(tmp_path)
    src = ContinueSource(sessions_dir=d)
    a = next(s for s in src.discover_sessions() if s.session_id.startswith("aaaa"))
    full = list(src.iter_records(a))
    assert len(full) == 4
    mid_offset = full[1][0]  # next-index after second record
    tail = list(src.iter_records(a, start_offset=mid_offset))
    assert len(tail) == 2
    # First tail record is the tool_result user envelope.
    assert tail[0][1].content_kind == "tool_result"
    assert isinstance(tail[1][1], SystemRecord)


def test_iter_records_assistant_string_content(tmp_path):
    d = _make_continue_tree(tmp_path)
    src = ContinueSource(sessions_dir=d)
    b = next(s for s in src.discover_sessions() if s.session_id.startswith("bbbb"))
    recs = [r for _, r in src.iter_records(b)]
    assert isinstance(recs[1], AssistantRecord)
    assert recs[1].content[0].type == "text"
    assert recs[1].content[0].text == "hi back"


def test_iter_records_malformed_file_yields_nothing(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "broken.json").write_text("{not json")
    src = ContinueSource(sessions_dir=d)
    sf = next(iter(src.discover_sessions()))
    assert list(src.iter_records(sf)) == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_adapter_in_registry_after_import():
    import lib.sources.continue_dev  # noqa: F401
    assert ContinueSource in SOURCES


def test_source_by_name_returns_continue():
    import lib.sources.continue_dev  # noqa: F401
    src = source_by_name("continue")
    assert isinstance(src, ContinueSource)


def test_all_sources_includes_continue():
    import lib.sources.continue_dev  # noqa: F401
    assert any(isinstance(s, ContinueSource) for s in all_sources())
