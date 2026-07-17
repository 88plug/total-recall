"""Tests for :mod:`lib.sources.gemini_cli`.

Coverage:
* discovery — projects-root layout, seed extraction, subagent dirs
* replay   — ``$set`` patches, ``$rewindTo`` truncation, last-write-wins
             collapse of repeated tool-call status updates
* content  — string vs PartListUnion (text / inlineData / functionCall /
             functionResponse)
* tokens   — Gemini ``{input, output, cached, thoughts, tool, total}``
             → Anthropic-shaped ``{input_tokens, ..., extras}``
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from lib.schema import AssistantRecord, SystemRecord, UserRecord
from lib.sources.base import SOURCES
from lib.sources.gemini_cli import (
    GeminiCliSource,
    _flatten_content,
    _map_tokens,
    _replay,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _build_session(
    tmp_root: Path,
    project_path: str = "/home/operator/proj",
    session_id: str = "abc12345",
    rows: list[dict] | None = None,
) -> Path:
    project_hash = hashlib.sha256(project_path.encode()).hexdigest()
    chats_dir = tmp_root / project_hash / "chats"
    file_name = f"session-2026-05-25T12-00-{session_id[:6]}.jsonl"
    target = chats_dir / file_name
    _write_jsonl(target, rows or [])
    return target


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_source_registered():
    assert GeminiCliSource in SOURCES
    assert GeminiCliSource.name == "gemini_cli"


# ---------------------------------------------------------------------------
# is_available / discovery
# ---------------------------------------------------------------------------


def test_is_available_false_when_missing(tmp_path: Path):
    s = GeminiCliSource(root=tmp_path / "does-not-exist")
    assert s.is_available() is False


def test_is_available_false_when_empty(tmp_path: Path):
    (tmp_path / "tmp").mkdir()
    s = GeminiCliSource(root=tmp_path / "tmp")
    assert s.is_available() is False


def test_discover_session_basic(tmp_path: Path):
    root = tmp_path / "tmp"
    metadata = {
        "sessionId": "sess-1",
        "projectHash": "deadbeef",
        "startTime": "2026-05-25T12:00:00Z",
        "lastUpdated": "2026-05-25T12:05:00Z",
        "directories": ["/home/operator/proj"],
        "cliVersion": "0.42.0",
    }
    msg = {
        "id": "m1",
        "timestamp": "2026-05-25T12:00:01Z",
        "type": "user",
        "content": "hello",
    }
    _build_session(root, session_id="sess-1", rows=[metadata, msg])
    s = GeminiCliSource(root=root)
    sessions = list(s.discover_sessions())
    assert len(sessions) == 1
    sf = sessions[0]
    assert sf.source == "gemini_cli"
    assert sf.session_id == "sess-1"
    assert sf.cwd == "/home/operator/proj"
    assert sf.started_at is not None
    assert sf.extra["is_subagent"] is False
    assert "projectHash" in sf.extra


def test_discover_includes_subagents(tmp_path: Path):
    root = tmp_path / "tmp"
    project_hash = hashlib.sha256(b"/x").hexdigest()
    chats = root / project_hash / "chats"
    _write_jsonl(
        chats / "session-2026-05-25T12-00-aaaaaa.jsonl",
        [{"sessionId": "parent", "projectHash": project_hash, "directories": ["/x"]}],
    )
    _write_jsonl(
        chats / "parent" / "child.jsonl",
        [{"sessionId": "child", "projectHash": project_hash, "directories": ["/x"]}],
    )
    s = GeminiCliSource(root=root)
    sessions = list(s.discover_sessions())
    assert len(sessions) == 2
    subs = [sf for sf in sessions if sf.extra.get("is_subagent")]
    assert len(subs) == 1
    assert subs[0].extra["parent_session_id"] == "parent"


# ---------------------------------------------------------------------------
# replay semantics
# ---------------------------------------------------------------------------


def test_replay_first_line_is_metadata(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(
        path,
        [
            {"sessionId": "s1", "startTime": "2026-05-25T12:00:00Z"},
            {"id": "m1", "type": "user", "content": "hi"},
        ],
    )
    meta, msgs = _replay(path)
    assert meta["sessionId"] == "s1"
    assert len(msgs) == 1
    assert msgs[0]["id"] == "m1"


def test_replay_set_patch_updates_metadata(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(
        path,
        [
            {"sessionId": "s1", "model": "old"},
            {"id": "m1", "type": "user", "content": "hi"},
            {"$set": {"model": "new", "lastUpdated": "2026-05-25T12:05:00Z"}},
        ],
    )
    meta, msgs = _replay(path)
    assert meta["model"] == "new"
    assert meta["lastUpdated"] == "2026-05-25T12:05:00Z"
    assert meta["sessionId"] == "s1"  # preserved
    assert len(msgs) == 1


def test_replay_rewind_truncates_messages(tmp_path: Path):
    path = tmp_path / "s.jsonl"
    _write_jsonl(
        path,
        [
            {"sessionId": "s1"},
            {"id": "m1", "type": "user", "content": "a"},
            {"id": "m2", "type": "gemini", "content": "b"},
            {"id": "m3", "type": "user", "content": "c"},
            {"$rewindTo": "m2"},  # nukes m2 and m3
            {"id": "m4", "type": "user", "content": "d"},
        ],
    )
    meta, msgs = _replay(path)
    ids = [m["id"] for m in msgs]
    assert ids == ["m1", "m4"]


def test_replay_last_write_wins_per_id(tmp_path: Path):
    """Repeated tool-call status updates must collapse — yield only the
    final state, in the original position."""
    path = tmp_path / "s.jsonl"
    _write_jsonl(
        path,
        [
            {"sessionId": "s1"},
            {
                "id": "g1",
                "type": "gemini",
                "content": "running tool",
                "toolCalls": [{"id": "t1", "name": "shell", "status": "pending"}],
            },
            {"id": "u1", "type": "user", "content": "ok"},
            {
                "id": "g1",  # rewrite of g1 — last write wins, position preserved
                "type": "gemini",
                "content": "running tool",
                "toolCalls": [
                    {
                        "id": "t1",
                        "name": "shell",
                        "status": "completed",
                        "result": "hello\n",
                    }
                ],
            },
        ],
    )
    meta, msgs = _replay(path)
    # Two unique messages, in original order; g1 still first.
    assert [m["id"] for m in msgs] == ["g1", "u1"]
    g1 = msgs[0]
    assert g1["toolCalls"][0]["status"] == "completed"
    assert g1["toolCalls"][0]["result"] == "hello\n"


# ---------------------------------------------------------------------------
# content / parts handling
# ---------------------------------------------------------------------------


def test_flatten_content_string():
    text, fcs, frs = _flatten_content("hi there")
    assert text == "hi there"
    assert fcs == [] and frs == []


def test_flatten_content_text_parts():
    parts = [{"text": "first"}, {"text": "second"}]
    text, fcs, frs = _flatten_content(parts)
    assert text == "first\nsecond"
    assert fcs == [] and frs == []


def test_flatten_content_skips_inline_data():
    parts = [
        {"text": "look:"},
        {"inlineData": {"mimeType": "image/png", "data": "BASE64DATA=="}},
        {"text": "neat huh"},
    ]
    text, fcs, frs = _flatten_content(parts)
    assert text == "look:\nneat huh"
    assert fcs == [] and frs == []


def test_flatten_content_function_call_and_response():
    parts = [
        {"text": "calling..."},
        {"functionCall": {"id": "c1", "name": "ls", "args": {"path": "/"}}},
        {"functionResponse": {"id": "c1", "name": "ls", "response": {"stdout": "x"}}},
    ]
    text, fcs, frs = _flatten_content(parts)
    assert text == "calling..."
    assert len(fcs) == 1 and fcs[0]["name"] == "ls"
    assert len(frs) == 1 and frs[0]["response"] == {"stdout": "x"}


# ---------------------------------------------------------------------------
# token remapping
# ---------------------------------------------------------------------------


def test_map_tokens_full():
    out = _map_tokens(
        {"input": 100, "output": 50, "cached": 30, "thoughts": 5, "tool": 7, "total": 192}
    )
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 50
    assert out["cache_read_tokens"] == 30
    assert out["cache_creation_tokens"] == 5
    assert out["extras"]["gemini_tool_tokens"] == 7
    assert out["extras"]["gemini_total_tokens"] == 192


def test_map_tokens_partial_and_missing():
    assert _map_tokens({"input": 1}) == {"input_tokens": 1}
    assert _map_tokens(None) == {}
    assert _map_tokens("bad") == {}


# ---------------------------------------------------------------------------
# end-to-end: synthetic .jsonl → translated Records
# ---------------------------------------------------------------------------


def test_iter_records_end_to_end(tmp_path: Path):
    """Synthetic session: metadata + 2 user + 2 gemini (one with toolCalls)
    + a ``$set`` + a ``$rewindTo`` — assert the final replayed Record
    sequence."""
    rows = [
        {
            "sessionId": "sX",
            "projectHash": "ph",
            "startTime": "2026-05-25T12:00:00Z",
            "directories": ["/home/operator/proj"],
            "model": "gemini-2.5-pro",
        },
        {"id": "u1", "type": "user", "timestamp": "t1", "content": "first turn"},
        {
            "id": "g1",
            "type": "gemini",
            "timestamp": "t2",
            "content": [{"text": "ok"}],
            "toolCalls": [{"id": "t1", "name": "shell", "status": "pending"}],
            "tokens": {
                "input": 10,
                "output": 3,
                "cached": 0,
                "thoughts": 0,
                "tool": 1,
                "total": 14,
            },
        },
        {"id": "u2", "type": "user", "timestamp": "t3", "content": "second turn"},
        # status update: same id, last-write-wins
        {
            "id": "g1",
            "type": "gemini",
            "timestamp": "t2",
            "content": [{"text": "ok"}],
            "toolCalls": [{"id": "t1", "name": "shell", "status": "completed", "result": "done"}],
            "tokens": {
                "input": 10,
                "output": 3,
                "cached": 0,
                "thoughts": 0,
                "tool": 1,
                "total": 14,
            },
        },
        {"$set": {"lastUpdated": "2026-05-25T12:09:00Z"}},
        {
            "id": "g2",
            "type": "gemini",
            "timestamp": "t4",
            "content": [{"text": "summary"}],
        },
        # add a doomed message then rewind it away
        {"id": "u_bad", "type": "user", "timestamp": "t5", "content": "noise"},
        {"$rewindTo": "u_bad"},
        # add an info row
        {"id": "i1", "type": "info", "timestamp": "t6", "content": "you compacted"},
    ]
    _build_session(tmp_path / "tmp", session_id="sX", rows=rows)
    s = GeminiCliSource(root=tmp_path / "tmp")
    sessions = list(s.discover_sessions())
    assert len(sessions) == 1
    yielded = list(s.iter_records(sessions[0]))
    cursors = [c for c, _ in yielded]
    recs = [r for _, r in yielded]
    # cursors are monotonically increasing 1-based
    assert cursors == sorted(cursors) and cursors[0] >= 1
    # Final ordered sequence: u1, g1(completed), u2, g2, i1
    assert [r.type for r in recs] == ["user", "assistant", "user", "assistant", "system"]
    # u1 / u2 carry text
    u1, g1, u2, g2, i1 = recs
    assert isinstance(u1, UserRecord) and u1.text == "first turn"
    assert isinstance(u2, UserRecord) and u2.text == "second turn"
    # g1 has the completed tool call (not the pending one)
    assert isinstance(g1, AssistantRecord)
    tool_blocks = [b for b in g1.content if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].tool_use.id == "t1"
    assert tool_blocks[0].raw.get("status") == "completed"
    # g1.usage was remapped
    assert g1.usage["input_tokens"] == 10
    assert g1.usage["output_tokens"] == 3
    assert g1.usage["extras"]["gemini_tool_tokens"] == 1
    # g2 is plain text
    assert isinstance(g2, AssistantRecord)
    assert any(b.type == "text" and b.text == "summary" for b in g2.content)
    # info → SystemRecord with subtype="info"
    assert isinstance(i1, SystemRecord)
    assert i1.subtype == "info"


def test_iter_records_function_call_and_response(tmp_path: Path):
    rows = [
        {"sessionId": "sFC", "directories": ["/p"]},
        {
            "id": "g1",
            "type": "gemini",
            "timestamp": "t1",
            "content": [
                {"text": "running"},
                {"functionCall": {"id": "c1", "name": "ls", "args": {"path": "/"}}},
            ],
        },
        {
            "id": "u_resp",
            "type": "user",
            "timestamp": "t2",
            "content": [
                {"functionResponse": {"id": "c1", "name": "ls", "response": "file_a\nfile_b"}}
            ],
        },
    ]
    _build_session(tmp_path / "tmp", session_id="sFC", rows=rows)
    s = GeminiCliSource(root=tmp_path / "tmp")
    sessions = list(s.discover_sessions())
    yielded = list(s.iter_records(sessions[0]))
    assert len(yielded) == 2
    g1 = yielded[0][1]
    u_resp = yielded[1][1]
    # gemini: text block + tool_use block from functionCall part
    assert isinstance(g1, AssistantRecord)
    kinds = [b.type for b in g1.content]
    assert "text" in kinds and "tool_use" in kinds
    tu = [b for b in g1.content if b.type == "tool_use"][0]
    assert tu.tool_use.name == "ls"
    assert tu.tool_use.input == {"path": "/"}
    # user with only functionResponse → tool_result content
    assert isinstance(u_resp, UserRecord)
    assert u_resp.content_kind == "tool_result"
    assert len(u_resp.tool_results) == 1
    assert u_resp.tool_results[0].tool_use_id == "c1"
    assert "file_a" in u_resp.tool_results[0].content


def test_iter_records_multimodal_skipped(tmp_path: Path):
    rows = [
        {"sessionId": "sM", "directories": ["/p"]},
        {
            "id": "u1",
            "type": "user",
            "timestamp": "t1",
            "content": [
                {"text": "what is this image"},
                {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
            ],
        },
    ]
    _build_session(tmp_path / "tmp", session_id="sM", rows=rows)
    s = GeminiCliSource(root=tmp_path / "tmp")
    sessions = list(s.discover_sessions())
    recs = [r for _, r in s.iter_records(sessions[0])]
    assert len(recs) == 1
    u1 = recs[0]
    assert isinstance(u1, UserRecord)
    # inlineData dropped; only the text survives
    assert u1.text == "what is this image"


def test_iter_records_skips_blank_and_bad_lines(tmp_path: Path):
    path = _build_session(
        tmp_path / "tmp",
        session_id="sBad",
        rows=[
            {"sessionId": "sBad", "directories": ["/p"]},
            {"id": "u1", "type": "user", "timestamp": "t1", "content": "ok"},
        ],
    )
    # append a blank line + malformed line — adapter must tolerate
    with path.open("a") as fh:
        fh.write("\n")
        fh.write("{not json\n")
        fh.write(json.dumps({"id": "u2", "type": "user", "content": "after"}) + "\n")

    s = GeminiCliSource(root=tmp_path / "tmp")
    sessions = list(s.discover_sessions())
    recs = [r for _, r in s.iter_records(sessions[0])]
    assert [r.type for r in recs] == ["user", "user"]


def test_iter_records_start_offset_skips(tmp_path: Path):
    rows = [
        {"sessionId": "sO", "directories": ["/p"]},
        {"id": "u1", "type": "user", "timestamp": "t1", "content": "one"},
        {"id": "u2", "type": "user", "timestamp": "t2", "content": "two"},
        {"id": "u3", "type": "user", "timestamp": "t3", "content": "three"},
    ]
    _build_session(tmp_path / "tmp", session_id="sO", rows=rows)
    s = GeminiCliSource(root=tmp_path / "tmp")
    sessions = list(s.discover_sessions())
    out = list(s.iter_records(sessions[0], start_offset=2))
    # should skip the first 2 → only "three" remains
    texts = [r.text for _, r in out]
    assert texts == ["three"]
