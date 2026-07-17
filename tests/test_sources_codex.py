"""Tests for :mod:`lib.sources.codex` — the OpenAI Codex CLI adapter.

All tests are hermetic — they build synthetic JSONL transcripts in
``tmp_path``. No real ``~/.codex/sessions`` is required.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.schema import (
    AssistantRecord,
    SystemRecord,
    UserRecord,
)
from lib.sources.base import SOURCES, source_by_name
from lib.sources.codex import CodexSource, _session_id_from_filename

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o))
            f.write("\n")


def _session_meta(sid: str, cwd: str = "/work/proj") -> dict:
    return {
        "timestamp": "2026-05-25T12:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": sid,
            "timestamp": "2026-05-25T12:00:00.000Z",
            "cwd": cwd,
            "originator": "codex-cli",
            "cli_version": "0.1.0",
            "source": "user",
            "model_provider": "openai",
        },
    }


def _turn_context(model: str, cwd: str = "/work/proj") -> dict:
    return {
        "timestamp": "2026-05-25T12:00:01.000Z",
        "type": "turn_context",
        "payload": {
            "model": model,
            "effort": "medium",
            "personality": "default",
            "approval_policy": "auto",
            "sandbox_policy": "workspace",
            "cwd": cwd,
        },
    }


def _response_message(role: str, text: str) -> dict:
    return {
        "timestamp": "2026-05-25T12:00:02.000Z",
        "type": "response_item",
        "payload": {"type": "Message", "role": role, "content": text},
    }


def _response_function_call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "timestamp": "2026-05-25T12:00:03.000Z",
        "type": "response_item",
        "payload": {
            "type": "FunctionCall",
            "name": name,
            "arguments": json.dumps(args),  # encoded twice on the wire
            "call_id": call_id,
        },
    }


def _response_function_call_output(call_id: str, output) -> dict:
    return {
        "timestamp": "2026-05-25T12:00:04.000Z",
        "type": "response_item",
        "payload": {
            "type": "FunctionCallOutput",
            "call_id": call_id,
            "output": output,
        },
    }


def _response_local_shell_call(cmd: list[str], call_id: str = "shell-1") -> dict:
    return {
        "timestamp": "2026-05-25T12:00:05.000Z",
        "type": "response_item",
        "payload": {
            "type": "LocalShellCall",
            "call_id": call_id,
            "action": {"command": cmd, "cwd": "/work"},
        },
    }


def _compacted(replacement_history: list[dict]) -> dict:
    return {
        "timestamp": "2026-05-25T12:00:06.000Z",
        "type": "compacted",
        "payload": {
            "summary": "compacted earlier turns",
            "replacement_history": replacement_history,
        },
    }


def _token_count_event(
    input_tokens: int = 100,
    cached_input_tokens: int = 30,
    output_tokens: int = 50,
    reasoning_output_tokens: int = 10,
    total_tokens: int = 190,
) -> dict:
    return {
        "timestamp": "2026-05-25T12:00:07.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "total_tokens": total_tokens,
            },
        },
    }


# ---------------------------------------------------------------------------
# registration / construction
# ---------------------------------------------------------------------------


def test_codex_registered():
    """Importing :mod:`lib.sources` triggers CodexSource registration."""
    import lib.sources  # noqa: F401

    names = [cls.name for cls in SOURCES]
    assert "codex" in names
    s = source_by_name("codex")
    assert s is not None
    assert s.name == "codex"


def test_codex_home_override_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    s = CodexSource()
    assert s.codex_home == tmp_path
    assert s.sessions_root == tmp_path / "sessions"


def test_codex_home_explicit_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", "/elsewhere")
    s = CodexSource(codex_home=tmp_path)
    assert s.codex_home == tmp_path


# ---------------------------------------------------------------------------
# is_available / discover_sessions
# ---------------------------------------------------------------------------


def test_is_available_missing(tmp_path):
    s = CodexSource(codex_home=tmp_path / "nope")
    assert s.is_available() is False


def test_is_available_empty(tmp_path):
    (tmp_path / "sessions").mkdir()
    s = CodexSource(codex_home=tmp_path)
    assert s.is_available() is False


def test_is_available_has_files(tmp_path):
    p = tmp_path / "sessions" / "2026" / "05" / "25"
    p.mkdir(parents=True)
    (p / "rollout-2026-05-25T12-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl").write_text("")
    s = CodexSource(codex_home=tmp_path)
    assert s.is_available() is True


def test_discover_sessions_walks_date_tree(tmp_path):
    base = tmp_path / "sessions"
    (base / "2026" / "05" / "24").mkdir(parents=True)
    (base / "2026" / "05" / "25").mkdir(parents=True)
    uuid_a = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    uuid_b = "11111111-2222-3333-4444-555555555555"
    fa = base / "2026" / "05" / "24" / f"rollout-2026-05-24T09-00-00-{uuid_a}.jsonl"
    fb = base / "2026" / "05" / "25" / f"rollout-2026-05-25T10-00-00-{uuid_b}.jsonl"
    fa.write_text("")
    fb.write_text("")

    s = CodexSource(codex_home=tmp_path)
    sessions = list(s.discover_sessions())
    assert len(sessions) == 2
    # Chronological because the path layout is date-sorted.
    assert sessions[0].path == fa
    assert sessions[1].path == fb
    assert sessions[0].session_id == uuid_a
    assert sessions[1].session_id == uuid_b
    assert sessions[0].source == "codex"


def test_session_id_filename_helper():
    name = "rollout-2026-05-25T12-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    assert _session_id_from_filename(name) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert _session_id_from_filename("garbage.jsonl") is None
    assert _session_id_from_filename("rollout-short.jsonl") is None


# ---------------------------------------------------------------------------
# iter_records — main translation behaviour
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, lines: list[dict]) -> Path:
    p = (
        tmp_path
        / "sessions"
        / "2026"
        / "05"
        / "25"
        / "rollout-2026-05-25T12-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    _write_jsonl(p, lines)
    return p


def test_iter_records_model_threading_with_mid_session_switch(tmp_path):
    """session_meta + 2 turn_context (model switch) + 3 response_items
    + 1 compacted + 2 more response_items → correct Record sequence with
    model field reflecting the most-recent turn_context."""
    lines = [
        _session_meta("sid-1", cwd="/work/proj"),
        _turn_context("gpt-5-codex", cwd="/work/proj"),
        _response_message("user", "first prompt"),
        _response_message("assistant", "first reply"),
        _response_function_call("apply_patch", {"path": "x.py"}, call_id="c1"),
        _turn_context("gpt-5-codex-high", cwd="/work/proj"),
        _compacted(
            replacement_history=[
                {"type": "Message", "role": "user", "content": "compacted prompt"},
            ]
        ),
        _response_message("assistant", "after-compact reply"),
        _response_function_call_output("c1", "all good"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))

    recs = [r for _, r in s.iter_records(sf)]
    # We expect: session_meta, turn_context, message(user), message(assistant),
    # function_call, turn_context, compacted_boundary, replayed message(user),
    # message(assistant), function_call_output.
    kinds = [type(r).__name__ for r in recs]
    assert kinds == [
        "SystemRecord",  # session_meta
        "SystemRecord",  # turn_context #1
        "UserRecord",  # first prompt
        "AssistantRecord",  # first reply (model = gpt-5-codex)
        "AssistantRecord",  # function_call (model = gpt-5-codex)
        "SystemRecord",  # turn_context #2 (model switch)
        "SystemRecord",  # compact_boundary
        "UserRecord",  # replayed prompt from replacement_history
        "AssistantRecord",  # after-compact reply (model = gpt-5-codex-high)
        "UserRecord",  # function_call_output
    ]

    # session_meta carried cwd + session_id forward
    assert recs[2].cwd == "/work/proj"
    assert recs[2].session_id == "sid-1"

    # model threading: pre-switch records show first model, post-switch show second.
    pre_assistant = recs[3]
    pre_function = recs[4]
    post_assistant = recs[8]
    assert isinstance(pre_assistant, AssistantRecord)
    assert isinstance(pre_function, AssistantRecord)
    assert isinstance(post_assistant, AssistantRecord)
    assert pre_assistant.model == "gpt-5-codex"
    assert pre_function.model == "gpt-5-codex"
    assert post_assistant.model == "gpt-5-codex-high"

    # compact boundary is tagged correctly
    boundary = recs[6]
    assert isinstance(boundary, SystemRecord)
    assert boundary.subtype == "compact_boundary"

    # turn_context #2 is tagged correctly
    tc2 = recs[5]
    assert isinstance(tc2, SystemRecord)
    assert tc2.subtype == "turn_context"
    assert tc2.payload["model"] == "gpt-5-codex-high"


def test_function_call_double_decode_roundtrip(tmp_path):
    """FunctionCall.arguments is a JSON-encoded string on the wire — the
    adapter must decode it twice so downstream sees a plain dict."""
    args = {"command": ["ls", "-la"], "cwd": "/work"}
    lines = [
        _session_meta("s2"),
        _turn_context("gpt-5"),
        _response_function_call("shell", args, call_id="call-xyz"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]

    fc = recs[-1]
    assert isinstance(fc, AssistantRecord)
    assert len(fc.content) == 1
    block = fc.content[0]
    assert block.type == "tool_use"
    assert block.tool_use is not None
    assert block.tool_use.name == "shell"
    assert block.tool_use.id == "call-xyz"
    # Critical: input is the decoded dict, not the JSON string.
    assert block.tool_use.input == args
    assert isinstance(block.tool_use.input["command"], list)


def test_function_call_with_namespace(tmp_path):
    lines = [
        _session_meta("s2b"),
        _turn_context("gpt-5"),
        {
            "timestamp": "2026-05-25T12:00:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "FunctionCall",
                "name": "search",
                "namespace": "mcp__docs",
                "arguments": json.dumps({"q": "x"}),
                "call_id": "c-ns",
            },
        },
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    fc = recs[-1]
    assert isinstance(fc, AssistantRecord)
    assert fc.content[0].tool_use.name == "mcp__docs.search"


def test_local_shell_call_separate_from_function_call(tmp_path):
    """LocalShellCall is its own variant — we still produce an AssistantRecord
    with a tool_use block, but tag the tool name as ``local_shell`` so the
    pipeline can distinguish it from a generic FunctionCall."""
    lines = [
        _session_meta("s3"),
        _turn_context("gpt-5"),
        _response_local_shell_call(["bash", "-c", "echo hi"], call_id="ls-1"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]

    ls = recs[-1]
    assert isinstance(ls, AssistantRecord)
    assert len(ls.content) == 1
    assert ls.content[0].type == "tool_use"
    assert ls.content[0].tool_use is not None
    assert ls.content[0].tool_use.name == "local_shell"
    assert ls.content[0].tool_use.id == "ls-1"
    assert ls.content[0].tool_use.input["command"] == ["bash", "-c", "echo hi"]


def test_function_call_output_string_shape(tmp_path):
    lines = [
        _session_meta("s4"),
        _turn_context("gpt-5"),
        _response_function_call_output("c1", "plain stdout"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    out = recs[-1]
    assert isinstance(out, UserRecord)
    assert out.content_kind == "tool_result"
    assert len(out.tool_results) == 1
    tr = out.tool_results[0]
    assert tr.tool_use_id == "c1"
    assert tr.content == "plain stdout"
    assert tr.raw_content == "plain stdout"


def test_function_call_output_structured_shape(tmp_path):
    """``{content_items: [{text: ...}, {text: ...}]}`` joins to a single string."""
    structured = {
        "content_items": [
            {"text": "line one"},
            {"text": "line two"},
        ],
    }
    lines = [
        _session_meta("s5"),
        _turn_context("gpt-5"),
        _response_function_call_output("c1", structured),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    out = recs[-1]
    assert isinstance(out, UserRecord)
    assert out.tool_results[0].content == "line one\nline two"
    assert out.tool_results[0].raw_content == structured
    # The dual-shape payload is also preserved under tool_use_result_payload.
    assert out.tool_use_result_payload == structured


def test_token_count_field_remapping(tmp_path):
    """token_count event_msg → SystemRecord with usage normalised to the
    cross-source field names."""
    lines = [
        _session_meta("s6"),
        _turn_context("gpt-5"),
        _token_count_event(
            input_tokens=200,
            cached_input_tokens=80,
            output_tokens=120,
            reasoning_output_tokens=15,
            total_tokens=415,
        ),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]

    tc = recs[-1]
    assert isinstance(tc, SystemRecord)
    assert tc.subtype == "event_msg:token_count"
    usage = tc.payload["usage"]
    assert usage["input_tokens"] == 200
    assert usage["cache_read_tokens"] == 80  # cached_input → cache_read
    assert usage["output_tokens"] == 120
    assert usage["cache_creation_tokens"] == 15  # reasoning_output → cache_creation
    assert usage["total_tokens"] == 415
    # And the raw OpenAI shape is preserved.
    assert usage["_codex_raw"]["cached_input_tokens"] == 80
    assert usage["_codex_raw"]["reasoning_output_tokens"] == 15
    assert "cache_creation_tokens" not in usage["_codex_raw"]


def test_iter_records_resume_via_offset(tmp_path):
    """Byte-offset checkpointing works the same as Claude Code."""
    lines = [
        _session_meta("s7"),
        _turn_context("gpt-5"),
        _response_message("user", "one"),
        _response_message("assistant", "two"),
        _response_message("user", "three"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))

    yields = list(s.iter_records(sf))
    assert len(yields) == 5

    # Resume from after the third yield (the assistant reply).
    mid = yields[2][0]
    yields2 = list(s.iter_records(sf, start_offset=mid))
    assert len(yields2) == 2
    assert isinstance(yields2[0][1], AssistantRecord)
    assert yields2[0][1].content and yields2[0][1].content[0].text == "two"


def test_iter_records_tolerates_truncated_tail(tmp_path):
    """A half-written final line is skipped — the writer will rewrite it."""
    p = (
        tmp_path
        / "sessions"
        / "2026"
        / "05"
        / "25"
        / "rollout-2026-05-25T12-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    p.parent.mkdir(parents=True)
    body = (
        json.dumps(_session_meta("s8"))
        + "\n"
        + json.dumps(_turn_context("gpt-5"))
        + "\n"
        + '{"timestamp":"...","type":"response_item","payload":{"type":"Mess'  # truncated
    )
    p.write_text(body, encoding="utf-8")

    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    # Only the two complete lines survive.
    assert len(recs) == 2


def test_iter_records_unknown_response_subvariant_falls_through(tmp_path):
    """Unknown response_item sub-variants become SystemRecords (not dropped),
    so byte-offset alignment is preserved and the raw payload is retained."""
    lines = [
        _session_meta("s9"),
        _turn_context("gpt-5"),
        {
            "timestamp": "2026-05-25T12:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "CustomToolCall",
                "name": "future-tool",
                "blob": {"x": 1},
            },
        },
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    last = recs[-1]
    assert isinstance(last, SystemRecord)
    assert last.subtype == "response_item:CustomToolCall"
    assert last.payload["blob"] == {"x": 1}


def test_reasoning_block_emits_thinking(tmp_path):
    lines = [
        _session_meta("s10"),
        _turn_context("gpt-5"),
        {
            "timestamp": "2026-05-25T12:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "Reasoning",
                "text": "step 1: ...",
            },
        },
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    asst = recs[-1]
    assert isinstance(asst, AssistantRecord)
    assert len(asst.content) == 1
    assert asst.content[0].type == "thinking"
    assert asst.content[0].thinking == "step 1: ..."


def test_session_meta_cwd_threads_into_subsequent_records(tmp_path):
    lines = [
        _session_meta("s11", cwd="/repo/A"),
        _turn_context("gpt-5", cwd="/repo/A"),
        _response_message("user", "hi"),
        _turn_context("gpt-5", cwd="/repo/B"),  # cwd changes mid-session too
        _response_message("user", "still here"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]

    # 0=meta, 1=turn_ctx, 2=user msg in /repo/A, 3=turn_ctx, 4=user msg in /repo/B
    assert recs[2].cwd == "/repo/A"
    assert recs[4].cwd == "/repo/B"


def test_session_id_falls_back_to_filename_for_pre_meta_lines(tmp_path):
    """If the first physical line is not session_meta, we still want a
    plausible session_id on what we yield — pulled from the filename."""
    lines = [
        # No session_meta at all — degenerate but observed in old rollouts.
        _turn_context("gpt-5"),
        _response_message("user", "orphan prompt"),
    ]
    _make_session(tmp_path, lines)
    s = CodexSource(codex_home=tmp_path)
    sf = next(iter(s.discover_sessions()))
    recs = [r for _, r in s.iter_records(sf)]
    # Filename UUID is the session id we expose.
    assert recs[0].session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert recs[1].session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
