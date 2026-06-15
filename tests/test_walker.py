"""Tests for :mod:`lib.jsonl_walker` and :mod:`lib.schema`.

Two layers:

1. Synthetic, hermetic tests that round-trip hand-crafted records — these
   run anywhere, including CI containers without ``~/.claude/projects``.
2. Corpus tests that parse 2-3 real session files. These are gated with
   ``pytest.skip`` when the projects directory is missing.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from lib.jsonl_walker import (
    DEFAULT_PROJECTS_ROOT,
    all_sessions,
    derive_cwd_from_slug,
    derive_session_id_from_path,
    derive_slug_from_cwd,
    iter_records,
    read_all,
    session_files_for_cwd,
)
from lib.schema import (
    AgentNameRecord,
    AssistantRecord,
    AttachmentRecord,
    PermissionModeRecord,
    SystemRecord,
    UserRecord,
)

# ---------------------------------------------------------------------------
# slug ↔ cwd
# ---------------------------------------------------------------------------


def test_derive_cwd_from_slug_basic():
    assert derive_cwd_from_slug("-home-operator-amnesia") == "/home/operator/amnesia"
    assert (
        derive_cwd_from_slug("-home-operator-ip-service-for-docker")
        == "/home/operator/ip/service/for/docker"
    )


def test_derive_slug_from_cwd_roundtrip():
    cwd = "/home/operator/amnesia"
    slug = derive_slug_from_cwd(cwd)
    assert slug == "-home-operator-amnesia"
    assert derive_cwd_from_slug(slug) == cwd


def test_derive_session_id_from_path():
    p = Path("/x/y/98f8dc13-0259-4a7d-9057-685697baa836.jsonl")
    assert derive_session_id_from_path(p) == "98f8dc13-0259-4a7d-9057-685697baa836"
    assert derive_session_id_from_path(Path("/x/short.jsonl")) is None


# ---------------------------------------------------------------------------
# synthetic round-trip
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o))
            f.write("\n")


def test_iter_records_skips_blanks_and_truncated_tail(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    # Two valid records, a blank line in between, and a truncated last line.
    body = (
        json.dumps({"type": "permission-mode", "permissionMode": "default"})
        + "\n\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": None,
                "sessionId": "s1",
                "timestamp": "2026-05-25T01:00:00.000Z",
                "cwd": "/home/x",
                "gitBranch": "main",
                "version": "2.1.150",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                        {
                            "type": "thinking",
                            "thinking": "secret",
                            "signature": "abc",
                        },
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "stop_reason": "tool_use",
                    "id": "msg_x",
                },
            }
        )
        + "\n"
        # Truncated final line — no terminating newline, JSON is incomplete.
        + '{"type":"user","uuid":"u1","parentUuid":"a1","message":{"role":"user","content":'
    )
    path.write_text(body, encoding="utf-8")

    yields = list(iter_records(path))
    # Permission-mode + assistant; truncated user dropped.
    assert len(yields) == 2

    (off0, r0), (off1, r1) = yields
    assert isinstance(r0, PermissionModeRecord)
    assert r0.permission_mode == "default"
    assert r0.byte_offset == 0

    assert isinstance(r1, AssistantRecord)
    assert r1.uuid == "a1"
    assert r1.model == "claude-opus-4-7"
    assert [b.type for b in r1.content] == ["text", "tool_use", "thinking"]
    assert r1.content[1].tool_use is not None
    assert r1.content[1].tool_use.name == "Bash"
    assert r1.content[2].thinking_signature == "abc"
    assert r1.stop_reason == "tool_use"
    # Byte-offset for the second record is past blank + first line.
    assert r1.byte_offset > 0
    # And the yielded next-offset matches the start of the truncated tail.
    assert off1 < len(body)


def test_iter_records_resume_via_offset(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "ai-title", "aiTitle": "first", "sessionId": "s"},
            {"type": "ai-title", "aiTitle": "second", "sessionId": "s"},
            {"type": "ai-title", "aiTitle": "third", "sessionId": "s"},
        ],
    )

    yields = list(iter_records(path))
    assert [r.raw["aiTitle"] for _, r in yields] == ["first", "second", "third"]
    mid = yields[0][0]  # next-offset after first record
    yields2 = list(iter_records(path, start_offset=mid))
    assert [r.raw["aiTitle"] for _, r in yields2] == ["second", "third"]


def test_user_record_polymorphic_content(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    _write_jsonl(
        path,
        [
            # string content (real prompt)
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "s",
                "message": {"role": "user", "content": "hello"},
            },
            # tool_result content
            {
                "type": "user",
                "uuid": "u2",
                "parentUuid": "u1",
                "sessionId": "s",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "is_error": False,
                            "content": "ok",
                        }
                    ],
                },
                "toolUseResult": {"stdout": "ok", "stderr": "", "interrupted": False},
            },
            # text array, with isMeta
            {
                "type": "user",
                "uuid": "u3",
                "parentUuid": "u2",
                "sessionId": "s",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "system reminder body"}],
                },
            },
            # isCompactSummary string
            {
                "type": "user",
                "uuid": "u4",
                "parentUuid": "u3",
                "sessionId": "s",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "summary text"},
            },
        ],
    )
    recs = read_all(path)
    assert all(isinstance(r, UserRecord) for r in recs)
    u1, u2, u3, u4 = recs
    assert u1.content_kind == "string" and u1.text == "hello"
    assert u2.content_kind == "tool_result"
    assert u2.tool_results[0].tool_use_id == "tu_1"
    assert u2.tool_results[0].content == "ok"
    assert (u2.tool_use_result_payload or {}).get("stdout") == "ok"
    assert u3.content_kind == "text_array" and u3.is_meta is True
    assert u4.is_compact_summary is True and u4.text == "summary text"


def test_attachment_discriminator_nested(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "attachment",
                "uuid": "a1",
                "parentUuid": None,
                "sessionId": "s",
                "attachment": {"type": "task_reminder", "content": "do the thing"},
            },
            {
                "type": "attachment",
                "uuid": "a2",
                "parentUuid": None,
                "sessionId": "s",
                "attachment": {"type": "skill_listing", "skills": []},
            },
        ],
    )
    recs = read_all(path)
    assert all(isinstance(r, AttachmentRecord) for r in recs)
    assert recs[0].attachment_type == "task_reminder"
    assert recs[1].attachment_type == "skill_listing"


def test_system_subtypes(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "system",
                "subtype": "turn_duration",
                "uuid": "sys1",
                "parentUuid": None,
                "sessionId": "s",
                "durationMs": 1234,
                "messageCount": 5,
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "sys2",
                "parentUuid": None,
                "logicalParentUuid": "x",
                "sessionId": "s",
                "compactMetadata": {
                    "trigger": "manual",
                    "preTokens": 100,
                    "postTokens": 10,
                    "preservedSegment": {
                        "headUuid": "h",
                        "anchorUuid": "a",
                        "tailUuid": "t",
                    },
                },
            },
        ],
    )
    recs = read_all(path)
    assert all(isinstance(r, SystemRecord) for r in recs)
    assert recs[0].subtype == "turn_duration"
    assert recs[0].payload["durationMs"] == 1234
    assert recs[1].subtype == "compact_boundary"
    assert recs[1].payload["compactMetadata"]["preservedSegment"]["headUuid"] == "h"


def test_agent_name_record_parses(tmp_path: Path):
    """``type:"agent-name"`` is a session-metadata record carrying just an
    agentName + sessionId — first-class as of the proxmox-plus corpus."""
    path = tmp_path / "x.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "agent-name",
                "agentName": "google-surveillance-blocking",
                "sessionId": "2edee928-21fc-4871-b4b4-faa5659be6b1",
            }
        ],
    )
    recs = read_all(path)
    assert len(recs) == 1
    rec = recs[0]
    assert isinstance(rec, AgentNameRecord)
    assert rec.type == "agent-name"
    assert rec.agent_name == "google-surveillance-blocking"
    assert rec.session_id == "2edee928-21fc-4871-b4b4-faa5659be6b1"
    # No uuid/parentUuid/timestamp expected on this record shape.
    assert rec.uuid is None
    assert rec.parent_uuid is None
    assert rec.ts is None
    # Raw is preserved for forward-compat.
    assert rec.raw["agentName"] == "google-surveillance-blocking"


def test_agent_name_record_missing_fields_defaults(tmp_path: Path):
    """Defensive parse: a malformed agent-name row still becomes the typed
    record (empty agent_name) rather than falling through to base Record."""
    path = tmp_path / "x.jsonl"
    _write_jsonl(path, [{"type": "agent-name", "sessionId": "s"}])
    recs = read_all(path)
    assert isinstance(recs[0], AgentNameRecord)
    assert recs[0].agent_name == ""


def test_unknown_type_falls_through(tmp_path: Path):
    """Unknown ``type`` values should not raise; they become a base Record."""
    path = tmp_path / "x.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "future-record-kind",
                "uuid": "z",
                "parentUuid": None,
                "sessionId": "s",
                "blob": {"foo": "bar"},
            }
        ],
    )
    recs = read_all(path)
    assert len(recs) == 1
    assert recs[0].type == "future-record-kind"
    assert recs[0].raw["blob"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------


def _have_corpus() -> bool:
    return DEFAULT_PROJECTS_ROOT.is_dir()


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_amnesia_session_parses_cleanly():
    path = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836.jsonl"
    )
    if not path.is_file():
        pytest.skip(f"reference session missing: {path}")

    counts: Counter[str] = Counter()
    total = 0
    last_offset = 0
    for off, rec in iter_records(path):
        total += 1
        counts[rec.type] += 1
        last_offset = off

    # We expect *something* of every common kind in this large session.
    assert total > 100
    for expected in ("assistant", "user", "system"):
        assert counts[expected] > 0, f"missing {expected!r} in real corpus"
    assert last_offset == path.stat().st_size or last_offset > 0


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_session_files_for_cwd():
    files = session_files_for_cwd(DEFAULT_PROJECTS_ROOT, "/home/operator/amnesia")
    if not files:
        pytest.skip("amnesia project not present")
    assert all(p.suffix == ".jsonl" for p in files)
    assert all(p.parent.name == "-home-operator-amnesia" for p in files)


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_all_sessions_yields_pairs():
    n = 0
    for cwd, path in all_sessions(DEFAULT_PROJECTS_ROOT):
        assert path.is_file()
        assert path.suffix == ".jsonl"
        assert cwd.startswith("/")
        n += 1
        if n >= 5:
            break
    assert n > 0


@pytest.mark.skipif(not _have_corpus(), reason="no ~/.claude/projects on this machine")
def test_corpus_first_line_cwd_may_be_null():
    """The known first-line gotcha: cwd/version/gitBranch start as None."""
    path = (
        DEFAULT_PROJECTS_ROOT
        / "-home-operator-amnesia"
        / "98f8dc13-0259-4a7d-9057-685697baa836.jsonl"
    )
    if not path.is_file():
        pytest.skip(f"reference session missing: {path}")
    it = iter_records(path)
    _, first = next(it)
    # Line 1 is permission-mode with null cwd.
    assert first.cwd is None or isinstance(first.cwd, str)
    # Walk until we find a non-null cwd; should happen within a handful of records.
    found = False
    for _, r in it:
        if r.cwd:
            assert r.cwd == "/home/operator/amnesia"
            found = True
            break
    assert found
