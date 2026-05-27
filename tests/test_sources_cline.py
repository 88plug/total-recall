"""Tests for the Cline adapter.

Hermetic: builds a synthetic Cline data tree under ``tmp_path``. No
``~/.cline`` or VS Code globalStorage is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.schema import AssistantRecord, Record, UserRecord
from lib.sources import SOURCES, SessionFile, all_sources, source_by_name
from lib.sources.cline import ClineSource


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _make_cline_tree(tmp_path: Path) -> Path:
    """Return a single Cline data dir with two tasks and a task-history index."""

    data_dir = tmp_path / "cline-data"

    # Task 1: text + tool_use + tool_result + final assistant text.
    t1 = data_dir / "tasks" / "task-001"
    api_1 = [
        {"role": "user", "content": "fix the bug"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "investigating"},
                {
                    "type": "tool_use",
                    "id": "tu-1",
                    "name": "read_file",
                    "input": {"path": "/repo/bug.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu-1",
                    "content": "file contents here",
                }
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "patched"}],
        },
    ]
    _write_json(t1 / "api_conversation_history.json", api_1)
    _write_json(t1 / "ui_messages.json", [])
    _write_json(
        t1 / "task_metadata.json",
        {
            "files_in_context": [],
            "model_usage": [
                {
                    "model_id": "claude-3-5-sonnet-20241022",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                {
                    "model_id": "claude-3-5-sonnet-20241022",
                    "input_tokens": 30,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 10,
                },
            ],
            "environment_history": [],
        },
    )

    # Task 2: minimal user-only canonical stream.
    t2 = data_dir / "tasks" / "task-002"
    _write_json(
        t2 / "api_conversation_history.json",
        [{"role": "user", "content": "just hi"}],
    )

    # Task 3 (dir present, no api_conv → must be skipped).
    t3 = data_dir / "tasks" / "task-003-empty"
    t3.mkdir(parents=True)

    # taskHistory.json index.
    _write_json(
        data_dir / "state" / "taskHistory.json",
        [
            {
                "id": "task-001",
                "ts": 1_700_000_000_000,
                "task": "fix the bug",
                "workspace": "/home/u/repo",
                "modelId": "claude-3-5-sonnet-20241022",
            },
            {
                "id": "task-002",
                "ts": 1_700_001_000_000,
                "task": "just hi",
                "workspace": "/home/u/other",
                "modelId": "claude-3-5-sonnet-20241022",
            },
        ],
    )

    return data_dir


# ---------------------------------------------------------------------------
# Identity / availability
# ---------------------------------------------------------------------------


def test_name_constant():
    assert ClineSource.name == "cline"
    assert ClineSource().name == "cline"


def test_default_data_dirs_includes_cli_path():
    src = ClineSource()
    assert (Path.home() / ".cline" / "data") in src.data_dirs


def test_default_data_dirs_includes_both_extension_ids():
    src = ClineSource()
    flat = [str(p) for p in src.data_dirs]
    assert any("cline.cline" in p for p in flat)
    assert any("saoudrizwan.claude-dev" in p for p in flat)


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLINE_HOME", str(tmp_path / "custom"))
    src = ClineSource()
    assert (tmp_path / "custom") in src.data_dirs


def test_is_available_false_when_missing(tmp_path):
    assert ClineSource(data_dirs=[tmp_path / "nope"]).is_available() is False


def test_is_available_true_with_tasks_dir(tmp_path):
    d = tmp_path / "cline-data"
    (d / "tasks").mkdir(parents=True)
    assert ClineSource(data_dirs=[d]).is_available() is True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_session_files(tmp_path):
    d = _make_cline_tree(tmp_path)
    src = ClineSource(data_dirs=[d])
    sessions = list(src.discover_sessions())

    assert len(sessions) == 2
    assert {s.source for s in sessions} == {"cline"}
    ids = [s.session_id for s in sessions]
    assert ids == sorted(ids)
    by_id = {s.session_id: s for s in sessions}
    t1 = by_id["task-001"]
    assert t1.cwd == "/home/u/repo"
    assert t1.extra.get("title") == "fix the bug"
    assert t1.extra.get("model") == "claude-3-5-sonnet-20241022"
    assert t1.extra.get("data_dir") == str(d)
    assert t1.started_at == pytest.approx(1_700_000_000.0)


def test_discover_skips_tasks_with_no_api_conv(tmp_path):
    d = _make_cline_tree(tmp_path)
    src = ClineSource(data_dirs=[d])
    ids = [s.session_id for s in src.discover_sessions()]
    assert "task-003-empty" not in ids


def test_discover_works_without_taskhistory(tmp_path):
    """No state/taskHistory.json — discovery still emits sessions, just
    without titles / model / workspace hints."""

    d = tmp_path / "cline-data"
    t = d / "tasks" / "lone-task"
    _write_json(t / "api_conversation_history.json", [])
    src = ClineSource(data_dirs=[d])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "lone-task"
    assert s.cwd is None
    assert "title" not in s.extra


def test_discover_merges_multiple_data_dirs(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_json(a / "tasks" / "from-a" / "api_conversation_history.json", [])
    _write_json(b / "tasks" / "from-b" / "api_conversation_history.json", [])
    src = ClineSource(data_dirs=[a, b])
    ids = {s.session_id for s in src.discover_sessions()}
    assert ids == {"from-a", "from-b"}


# ---------------------------------------------------------------------------
# Record streaming
# ---------------------------------------------------------------------------


def test_iter_records_projects_anthropic_messages(tmp_path):
    d = _make_cline_tree(tmp_path)
    src = ClineSource(data_dirs=[d])
    t1 = next(s for s in src.discover_sessions() if s.session_id == "task-001")
    records = [r for _, r in src.iter_records(t1)]
    assert len(records) == 4

    # 0: user text prompt.
    assert isinstance(records[0], UserRecord)
    assert records[0].text == "fix the bug"
    assert records[0].cwd == "/home/u/repo"

    # 1: assistant with text + tool_use, model + summed usage populated.
    assert isinstance(records[1], AssistantRecord)
    assert records[1].model == "claude-3-5-sonnet-20241022"
    types = [b.type for b in records[1].content]
    assert "text" in types and "tool_use" in types
    tu = next(b.tool_use for b in records[1].content if b.type == "tool_use")
    assert tu.id == "tu-1"
    assert tu.name == "read_file"
    assert tu.input == {"path": "/repo/bug.py"}
    # Summed: 100+30=130 input, 50+20=70 output, 10 cache_read.
    assert records[1].usage == {
        "input_tokens": 130,
        "output_tokens": 70,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 10,
    }

    # 2: user tool_result envelope.
    assert isinstance(records[2], UserRecord)
    assert records[2].content_kind == "tool_result"
    assert len(records[2].tool_results) == 1
    assert records[2].tool_results[0].tool_use_id == "tu-1"
    assert records[2].tool_results[0].content == "file contents here"

    # 3: second assistant — no usage (only first gets it).
    assert isinstance(records[3], AssistantRecord)
    assert records[3].usage is None


def test_iter_records_respects_start_offset(tmp_path):
    d = _make_cline_tree(tmp_path)
    src = ClineSource(data_dirs=[d])
    t1 = next(s for s in src.discover_sessions() if s.session_id == "task-001")
    full = list(src.iter_records(t1))
    assert len(full) == 4
    mid = full[1][0]
    tail = list(src.iter_records(t1, start_offset=mid))
    assert len(tail) == 2
    assert tail[0][1].content_kind == "tool_result"  # the user/tool_result rec


def test_iter_records_handles_string_assistant_content(tmp_path):
    d = tmp_path / "cline-data"
    t = d / "tasks" / "str-task"
    _write_json(
        t / "api_conversation_history.json",
        [{"role": "assistant", "content": "plain string"}],
    )
    src = ClineSource(data_dirs=[d])
    sf = next(iter(src.discover_sessions()))
    rec = list(src.iter_records(sf))[0][1]
    assert isinstance(rec, AssistantRecord)
    assert rec.content[0].type == "text"
    assert rec.content[0].text == "plain string"


def test_iter_records_no_metadata_no_usage(tmp_path):
    d = tmp_path / "cline-data"
    t = d / "tasks" / "lean"
    _write_json(
        t / "api_conversation_history.json",
        [{"role": "assistant", "content": "ok"}],
    )
    src = ClineSource(data_dirs=[d])
    sf = next(iter(src.discover_sessions()))
    rec = list(src.iter_records(sf))[0][1]
    assert isinstance(rec, AssistantRecord)
    assert rec.usage is None


def test_iter_records_uses_session_workspace_as_cwd(tmp_path):
    d = _make_cline_tree(tmp_path)
    src = ClineSource(data_dirs=[d])
    t2 = next(s for s in src.discover_sessions() if s.session_id == "task-002")
    recs = [r for _, r in src.iter_records(t2)]
    assert recs[0].cwd == "/home/u/other"


def test_iter_records_falls_back_to_metadata_for_model(tmp_path):
    """If taskHistory.json lacks ``modelId``, model comes from task_metadata."""

    d = tmp_path / "cline-data"
    t = d / "tasks" / "no-model-hint"
    _write_json(
        t / "api_conversation_history.json",
        [{"role": "assistant", "content": [{"type": "text", "text": "x"}]}],
    )
    _write_json(
        t / "task_metadata.json",
        {"model_usage": [{"model_id": "from-metadata", "input_tokens": 0}]},
    )
    # No taskHistory.json at all → no model hint from extra.
    src = ClineSource(data_dirs=[d])
    sf = next(iter(src.discover_sessions()))
    rec = list(src.iter_records(sf))[0][1]
    assert isinstance(rec, AssistantRecord)
    assert rec.model == "from-metadata"


def test_iter_records_malformed_file_yields_nothing(tmp_path):
    d = tmp_path / "cline-data"
    t = d / "tasks" / "broken"
    t.mkdir(parents=True)
    (t / "api_conversation_history.json").write_text("{not json")
    src = ClineSource(data_dirs=[d])
    sf = next(iter(src.discover_sessions()))
    assert list(src.iter_records(sf)) == []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_adapter_in_registry_after_import():
    import lib.sources.cline  # noqa: F401
    assert ClineSource in SOURCES


def test_source_by_name_returns_cline():
    import lib.sources.cline  # noqa: F401
    src = source_by_name("cline")
    assert isinstance(src, ClineSource)


def test_all_sources_includes_cline():
    import lib.sources.cline  # noqa: F401
    assert any(isinstance(s, ClineSource) for s in all_sources())
