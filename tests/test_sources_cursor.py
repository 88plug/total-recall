"""Tests for :mod:`lib.sources.cursor`.

Covers JSONL (v1) and vscdb SQLite (v2) paths.

Hermetic: every test builds fake trees/DBs under ``tmp_path``.
The real ``~/.cursor`` and Cursor user-data dirs are never touched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lib.schema import AssistantRecord, Record, UserRecord
from lib.sources.base import SessionFile
from lib.sources.cursor import (
    CursorSource,
    _cursor_line_to_record,
    _resolve_cwd_for_vscdb,
    _discover_vscdb_paths,
    _cursor_user_bases,
    _extract_bubble_text,
    _bubble_to_record,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_cursor_home(
    root: Path,
    proj_hash: str,
    session_name: str,
    lines: list[Any],
    *,
    trailing_blank: bool = True,
) -> Path:
    """Build a fake ``~/.cursor`` tree with one session and return root."""
    transcripts = root / "projects" / proj_hash / "agent-transcripts"
    transcripts.mkdir(parents=True)
    sf = transcripts / f"{session_name}.jsonl"
    with sf.open("w") as fh:
        for ln in lines:
            if isinstance(ln, (dict, list)):
                fh.write(json.dumps(ln) + "\n")
            else:
                # Raw text — used for malformed-line tests.
                fh.write(str(ln) + "\n")
        if trailing_blank:
            fh.write("\n")
    return root


@pytest.fixture
def cursor_home(tmp_path: Path) -> Path:
    """A populated fake ``~/.cursor`` with one project and one transcript."""
    return _make_cursor_home(
        tmp_path,
        proj_hash="a1b2c3d4e5f6deadbeefcafebabe1234",
        session_name="2026-05-24T10-00-00_session",
        lines=[
            {
                "id": "msg-1",
                "timestamp": "2026-05-24T10:00:00Z",
                "role": "user",
                "content": "list the relays",
            },
            {
                "id": "msg-2",
                "timestamp": "2026-05-24T10:00:01Z",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [
                    {"type": "text", "text": "running wg show"}
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            {
                "id": "msg-3",
                "timestamp": 1716545402.5,  # epoch seconds form
                "role": "tool",
                "tool_call_id": "call-xyz",
                "content": "peer1\npeer2\n",
            },
        ],
    )


# ---------------------------------------------------------------------------
# Adapter wiring
# ---------------------------------------------------------------------------


def test_registered_in_sources():
    import lib.sources  # noqa: F401 — trigger registration of bundled adapters
    import lib.sources.cursor  # noqa: F401 — explicitly ensure cursor is loaded

    from lib.sources.base import SOURCES

    names = [cls.name for cls in SOURCES]
    assert "cursor" in names


def test_class_name_constant():
    assert CursorSource.name == "cursor"


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


def test_is_available_false_when_no_cursor_home(tmp_path: Path):
    src = CursorSource(cursor_home=tmp_path / "nope")
    assert src.is_available() is False


def test_is_available_false_when_projects_root_empty(tmp_path: Path):
    (tmp_path / "projects").mkdir()
    src = CursorSource(cursor_home=tmp_path)
    assert src.is_available() is False


def test_is_available_false_when_project_has_no_transcripts(tmp_path: Path):
    (tmp_path / "projects" / "deadbeef").mkdir(parents=True)
    src = CursorSource(cursor_home=tmp_path)
    assert src.is_available() is False


def test_is_available_true_with_transcripts(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    assert src.is_available() is True


def test_is_available_ignores_non_dir_entries(tmp_path: Path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "stray-file").write_text("noise")
    src = CursorSource(cursor_home=tmp_path)
    assert src.is_available() is False


# ---------------------------------------------------------------------------
# discover_sessions
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_handles(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert isinstance(s, SessionFile)
    assert s.source == "cursor"
    assert s.session_id == "2026-05-24T10-00-00_session"
    assert s.cwd is None  # opaque hash — operator must map manually
    assert s.extra["projHash"] == "a1b2c3d4e5f6deadbeefcafebabe1234"
    assert s.extra["unresolved_cwd"] is True
    assert s.started_at is None
    assert s.last_modified > 0


def test_discover_sessions_empty_when_unavailable(tmp_path: Path):
    src = CursorSource(cursor_home=tmp_path / "absent")
    assert list(src.discover_sessions()) == []


def test_discover_sessions_skips_projects_without_transcripts(tmp_path: Path):
    # One project with transcripts.
    _make_cursor_home(
        tmp_path,
        proj_hash="aaa",
        session_name="s1",
        lines=[{"id": "1", "role": "user", "content": "hi"}],
    )
    # Another project without an agent-transcripts dir.
    (tmp_path / "projects" / "bbb").mkdir()
    src = CursorSource(cursor_home=tmp_path)
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    assert sessions[0].extra["projHash"] == "aaa"


def test_discover_sessions_sort_order_stable(tmp_path: Path):
    # Two projects, two transcripts each — order must be project-then-file.
    for proj in ("ccc", "aaa", "bbb"):
        _make_cursor_home(
            tmp_path,
            proj_hash=proj,
            session_name=f"{proj}-z",
            lines=[{"id": "1", "role": "user", "content": "x"}],
        )
        # Add a second session in the same project.
        extra = tmp_path / "projects" / proj / "agent-transcripts" / f"{proj}-a.jsonl"
        with extra.open("w") as fh:
            fh.write(json.dumps({"id": "2", "role": "user", "content": "y"}) + "\n")
    src = CursorSource(cursor_home=tmp_path)
    sessions = list(src.discover_sessions())
    sigs = [(s.extra["projHash"], s.session_id) for s in sessions]
    assert sigs == [
        ("aaa", "aaa-a"),
        ("aaa", "aaa-z"),
        ("bbb", "bbb-a"),
        ("bbb", "bbb-z"),
        ("ccc", "ccc-a"),
        ("ccc", "ccc-z"),
    ]


# ---------------------------------------------------------------------------
# iter_records — translation
# ---------------------------------------------------------------------------


def _records(src: CursorSource, session: SessionFile) -> list[tuple[int, Record]]:
    return list(src.iter_records(session))


def test_iter_records_translates_user_assistant_tool(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    assert len(recs) == 3

    # Each yielded byte offset is monotonically increasing.
    offsets = [off for off, _ in recs]
    assert offsets == sorted(offsets)
    assert all(a < b for a, b in zip(offsets, offsets[1:]))

    _, user = recs[0]
    assert isinstance(user, UserRecord)
    assert user.type == "user"
    assert user.text == "list the relays"
    assert user.content_kind == "string"
    assert user.uuid == "msg-1"

    _, asst = recs[1]
    assert isinstance(asst, AssistantRecord)
    assert asst.model == "claude-opus-4-7"
    assert asst.usage == {"input_tokens": 10, "output_tokens": 5}
    assert len(asst.content) == 1
    assert asst.content[0].type == "text"
    assert asst.content[0].text == "running wg show"

    _, tool = recs[2]
    assert isinstance(tool, UserRecord)
    assert tool.type == "tool"
    assert tool.content_kind == "tool_result"
    assert len(tool.tool_results) == 1
    assert tool.tool_results[0].tool_use_id == "call-xyz"
    assert tool.tool_results[0].content == "peer1\npeer2\n"
    # Epoch-second timestamp parsed.
    assert tool.ts is not None


def test_iter_records_skips_blank_lines(tmp_path: Path):
    transcripts = tmp_path / "projects" / "x" / "agent-transcripts"
    transcripts.mkdir(parents=True)
    sf = transcripts / "s.jsonl"
    with sf.open("w") as fh:
        fh.write("\n")
        fh.write(json.dumps({"id": "1", "role": "user", "content": "a"}) + "\n")
        fh.write("\n\n")
        fh.write(json.dumps({"id": "2", "role": "user", "content": "b"}) + "\n")
    src = CursorSource(cursor_home=tmp_path)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    assert [r.uuid for _, r in recs] == ["1", "2"]


def test_iter_records_skips_malformed_lines(tmp_path: Path):
    transcripts = tmp_path / "projects" / "x" / "agent-transcripts"
    transcripts.mkdir(parents=True)
    sf = transcripts / "s.jsonl"
    with sf.open("w") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"id": "1", "role": "user", "content": "ok"}) + "\n")
        fh.write("{broken: json,,\n")
        fh.write(json.dumps([1, 2, 3]) + "\n")  # non-dict top-level
        fh.write(json.dumps({"id": "2", "role": "user", "content": "ok2"}) + "\n")
    src = CursorSource(cursor_home=tmp_path)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    assert [r.uuid for _, r in recs] == ["1", "2"]


def test_iter_records_skips_truncated_tail(tmp_path: Path):
    transcripts = tmp_path / "projects" / "x" / "agent-transcripts"
    transcripts.mkdir(parents=True)
    sf = transcripts / "s.jsonl"
    with sf.open("w") as fh:
        fh.write(json.dumps({"id": "1", "role": "user", "content": "ok"}) + "\n")
        # No trailing newline — must be dropped, not raised on.
        fh.write('{"id": "2", "role": "user", "content": "partial"')
    src = CursorSource(cursor_home=tmp_path)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    assert [r.uuid for _, r in recs] == ["1"]


def test_iter_records_unknown_role_falls_through_to_base(tmp_path: Path):
    transcripts = tmp_path / "projects" / "x" / "agent-transcripts"
    transcripts.mkdir(parents=True)
    sf = transcripts / "s.jsonl"
    with sf.open("w") as fh:
        fh.write(json.dumps({"id": "1", "role": "system", "content": "x"}) + "\n")
        # Missing role at all.
        fh.write(json.dumps({"id": "2", "content": "no role"}) + "\n")
    src = CursorSource(cursor_home=tmp_path)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    types = [r.type for _, r in recs]
    assert types == ["system", "?"]
    # Both fall through to base Record (not Assistant/User/etc).
    for _, r in recs:
        assert type(r) is Record


def test_iter_records_resume_from_offset(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    session = next(iter(src.discover_sessions()))
    all_recs = _records(src, session)
    # Resume after the first record.
    first_offset = all_recs[0][0]
    resumed = list(src.iter_records(session, start_offset=first_offset))
    assert [r.uuid for _, r in resumed] == [r.uuid for _, r in all_recs[1:]]


def test_iter_records_preserves_raw(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    # Every Record's raw should round-trip the original line keys.
    assert recs[0][1].raw["role"] == "user"
    assert recs[1][1].raw["model"] == "claude-opus-4-7"
    assert recs[2][1].raw["tool_call_id"] == "call-xyz"


def test_iter_records_session_id_propagates(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    session = next(iter(src.discover_sessions()))
    for _, r in src.iter_records(session):
        assert r.session_id == session.session_id


def test_iter_records_byte_offset_on_record(cursor_home: Path):
    src = CursorSource(cursor_home=cursor_home)
    session = next(iter(src.discover_sessions()))
    recs = _records(src, session)
    # First record starts at byte 0; second starts at next_offset of first.
    assert recs[0][1].byte_offset == 0
    assert recs[1][1].byte_offset == recs[0][0]


# ---------------------------------------------------------------------------
# _cursor_line_to_record — direct unit tests
# ---------------------------------------------------------------------------


def _fake_session(cwd: str | None = None) -> SessionFile:
    return SessionFile(
        source="cursor",
        path=Path("/tmp/fake.jsonl"),
        cwd=cwd,
        session_id="sess-1",
        started_at=None,
        last_modified=0.0,
        extra={"projHash": "abc", "unresolved_cwd": True},
    )


def test_translate_assistant_string_content():
    """Cursor sometimes emits assistant content as a bare string."""
    sess = _fake_session()
    rec = _cursor_line_to_record(
        {"id": "x", "role": "assistant", "content": "hello"}, sess
    )
    assert isinstance(rec, AssistantRecord)
    assert len(rec.content) == 1
    assert rec.content[0].type == "text"
    assert rec.content[0].text == "hello"


def test_translate_tool_with_error_flag():
    sess = _fake_session()
    rec = _cursor_line_to_record(
        {
            "id": "t1",
            "role": "tool",
            "tool_call_id": "c1",
            "is_error": True,
            "content": "boom",
        },
        sess,
    )
    assert isinstance(rec, UserRecord)
    assert rec.content_kind == "tool_result"
    assert rec.tool_results[0].is_error is True
    assert rec.tool_results[0].content == "boom"


def test_translate_tool_alt_id_keys():
    """Cursor has used several keys for the tool call id; all should work."""
    sess = _fake_session()
    for key in ("tool_call_id", "toolCallId", "tool_use_id"):
        rec = _cursor_line_to_record(
            {"id": "t", "role": "tool", key: "abc", "content": "out"}, sess
        )
        assert isinstance(rec, UserRecord)
        assert rec.tool_results[0].tool_use_id == "abc"


def test_translate_epoch_and_iso_timestamps():
    sess = _fake_session()
    iso = _cursor_line_to_record(
        {"id": "1", "role": "user", "content": "x", "timestamp": "2026-05-24T10:00:00Z"},
        sess,
    )
    epoch = _cursor_line_to_record(
        {"id": "2", "role": "user", "content": "x", "timestamp": 1716545400},
        sess,
    )
    bad = _cursor_line_to_record(
        {"id": "3", "role": "user", "content": "x", "timestamp": {"bad": "shape"}},
        sess,
    )
    assert iso.ts is not None
    assert epoch.ts is not None
    assert bad.ts is None


def test_translate_propagates_session_cwd(cursor_home: Path):
    """If the operator did manage to set cwd on the SessionFile, use it."""
    sess = _fake_session(cwd="/home/operator/myproj")
    rec = _cursor_line_to_record(
        {"id": "1", "role": "user", "content": "x"}, sess
    )
    assert rec.cwd == "/home/operator/myproj"
    assert rec.session_id == "sess-1"


def test_translate_non_string_id_coerced():
    sess = _fake_session()
    rec = _cursor_line_to_record(
        {"id": 42, "role": "user", "content": "x"}, sess
    )
    assert rec.uuid == "42"


# ===========================================================================
# vscdb SQLite path (v2)
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers: synthetic vscdb builders
# ---------------------------------------------------------------------------

_COMPOSER_ID = "composer-abc123"
_BUBBLE_USER_ID = "bubble-u1"
_BUBBLE_ASST_ID = "bubble-a1"


def _make_vscdb(
    root: Path,
    subdir: str = "globalStorage",
    *,
    composer_meta: dict | None = None,
    bubbles: list[dict] | None = None,
    workspace_json: dict | None = None,
    item_table_only: bool = False,
) -> Path:
    """Build a synthetic Cursor state.vscdb under ``root / subdir /``."""
    db_dir = root / subdir
    db_dir.mkdir(parents=True, exist_ok=True)
    vscdb = db_dir / "state.vscdb"

    if workspace_json is not None:
        (db_dir / "workspace.json").write_text(json.dumps(workspace_json))

    conn = sqlite3.connect(str(vscdb))

    if item_table_only:
        # Legacy workspace DB — only ItemTable
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                "workbench.panel.aichat.view.aichat.chatdata",
                json.dumps({
                    "tabs": [
                        {
                            "id": "tab-1",
                            "bubbles": [
                                {"type": 1, "text": "hello from legacy", "bubbleId": "lb-1"},
                                {"type": 2, "text": "hi back", "bubbleId": "lb-2"},
                            ],
                        }
                    ]
                }),
            ),
        )
        conn.commit()
        conn.close()
        return vscdb

    # Modern DB — cursorDiskKV
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
    )

    meta = composer_meta or {
        "name": "Test Session",
        "createdAt": "2026-05-24T10:00:00Z",
        "updatedAt": "2026-05-24T10:05:00Z",
    }
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"composerData:{_COMPOSER_ID}", json.dumps(meta)),
    )

    default_bubbles = [
        {
            "bubbleId": _BUBBLE_USER_ID,
            "type": 1,  # user
            "text": "list the relays",
            "createdAt": "2026-05-24T10:01:00Z",
        },
        {
            "bubbleId": _BUBBLE_ASST_ID,
            "type": 2,  # assistant
            "text": "running wg show",
            "createdAt": "2026-05-24T10:01:05Z",
            "modelInfo": {"modelName": "claude-opus-4-7"},
            "tokenCount": {"inputTokens": 10, "outputTokens": 5},
        },
    ]
    for b in (bubbles or default_bubbles):
        bid = b.get("bubbleId", "unknown")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"bubbleId:{_COMPOSER_ID}:{bid}", json.dumps(b)),
        )

    conn.commit()
    conn.close()
    return vscdb


def _vscdb_source(vscdb_paths: list[Path]) -> CursorSource:
    """CursorSource with no JSONL tree and explicit vscdb paths."""
    src = CursorSource(cursor_home=Path("/nonexistent"), vscdb_paths=vscdb_paths)
    return src


# ---------------------------------------------------------------------------
# _resolve_cwd_for_vscdb
# ---------------------------------------------------------------------------


def test_resolve_cwd_folder_key(tmp_path: Path):
    vscdb = tmp_path / "w" / "state.vscdb"
    vscdb.parent.mkdir(parents=True)
    vscdb.touch()
    (vscdb.parent / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/operator/myproj"})
    )
    assert _resolve_cwd_for_vscdb(vscdb) == "/home/operator/myproj"


def test_resolve_cwd_workspace_key(tmp_path: Path):
    vscdb = tmp_path / "w" / "state.vscdb"
    vscdb.parent.mkdir(parents=True)
    vscdb.touch()
    (vscdb.parent / "workspace.json").write_text(
        json.dumps({"workspace": "file:///home/operator/foo.code-workspace"})
    )
    assert _resolve_cwd_for_vscdb(vscdb) == "/home/operator/foo.code-workspace"


def test_resolve_cwd_url_encoded(tmp_path: Path):
    vscdb = tmp_path / "w" / "state.vscdb"
    vscdb.parent.mkdir(parents=True)
    vscdb.touch()
    (vscdb.parent / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/operator/my%20project"})
    )
    assert _resolve_cwd_for_vscdb(vscdb) == "/home/operator/my project"


def test_resolve_cwd_absent(tmp_path: Path):
    vscdb = tmp_path / "w" / "state.vscdb"
    vscdb.parent.mkdir(parents=True)
    vscdb.touch()
    assert _resolve_cwd_for_vscdb(vscdb) is None


def test_resolve_cwd_malformed_json(tmp_path: Path):
    vscdb = tmp_path / "w" / "state.vscdb"
    vscdb.parent.mkdir(parents=True)
    vscdb.touch()
    (vscdb.parent / "workspace.json").write_text("not json at all")
    assert _resolve_cwd_for_vscdb(vscdb) is None


# ---------------------------------------------------------------------------
# _discover_vscdb_paths (platform path logic)
# ---------------------------------------------------------------------------


def test_discover_vscdb_paths_layout(tmp_path: Path):
    """_discover_vscdb_paths scans globalStorage + workspaceStorage under each base."""
    # Build a fake user base layout
    base = tmp_path / "CursorUser"
    gs = base / "globalStorage"
    gs.mkdir(parents=True)
    (gs / "state.vscdb").touch()
    ws = base / "workspaceStorage" / "abc123"
    ws.mkdir(parents=True)
    (ws / "state.vscdb").touch()

    with patch("lib.sources.cursor._cursor_user_bases", return_value=[base]):
        paths = _discover_vscdb_paths()

    assert any(p.name == "state.vscdb" and "globalStorage" in str(p) for p in paths)
    assert any(p.name == "state.vscdb" and "workspaceStorage" in str(p) for p in paths)


# ---------------------------------------------------------------------------
# is_available with vscdb paths
# ---------------------------------------------------------------------------


def test_is_available_vscdb_only(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path)
    src = _vscdb_source([vscdb])
    assert src.is_available() is True


def test_is_available_false_when_no_jsonl_and_no_vscdb(tmp_path: Path):
    src = CursorSource(cursor_home=tmp_path / "nope", vscdb_paths=[])
    assert src.is_available() is False


# ---------------------------------------------------------------------------
# discover_sessions — cursorDiskKV path
# ---------------------------------------------------------------------------


def test_discover_sessions_vscdb_yields_sessions(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path)
    src = _vscdb_source([vscdb])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert isinstance(s, SessionFile)
    assert s.source == "cursor"
    assert s.session_id == _COMPOSER_ID
    assert s.extra["storage"] == "vscdb"
    assert s.extra["composer_id"] == _COMPOSER_ID
    assert s.extra["vscdb_path"] == str(vscdb)


def test_discover_sessions_vscdb_cwd_from_workspace_json(tmp_path: Path):
    vscdb = _make_vscdb(
        tmp_path,
        workspace_json={"folder": "file:///home/operator/relay-project"},
    )
    src = _vscdb_source([vscdb])
    sessions = list(src.discover_sessions())
    assert sessions[0].cwd == "/home/operator/relay-project"
    assert "unresolved_cwd" not in sessions[0].extra


def test_discover_sessions_vscdb_no_workspace_json_sets_unresolved(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path)
    src = _vscdb_source([vscdb])
    sessions = list(src.discover_sessions())
    assert sessions[0].cwd is None
    assert sessions[0].extra.get("unresolved_cwd") is True


def test_discover_sessions_vscdb_started_at_parsed(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path, composer_meta={"createdAt": "2026-05-24T10:00:00Z"})
    src = _vscdb_source([vscdb])
    sessions = list(src.discover_sessions())
    assert sessions[0].started_at is not None
    assert sessions[0].started_at > 0


def test_discover_sessions_unreadable_vscdb_skipped(tmp_path: Path):
    fake = tmp_path / "state.vscdb"
    fake.write_text("not a sqlite db")
    src = _vscdb_source([fake])
    sessions = list(src.discover_sessions())
    # Should not raise; returns empty (or logs warning)
    assert isinstance(sessions, list)


def test_discover_sessions_multiple_vscdb(tmp_path: Path):
    vscdb1 = _make_vscdb(tmp_path / "gs1", subdir=".")
    vscdb2 = _make_vscdb(tmp_path / "gs2", subdir=".")
    # Each DB has one composerData, so 2 sessions total
    src = _vscdb_source([vscdb1, vscdb2])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 2


# ---------------------------------------------------------------------------
# iter_records — cursorDiskKV path
# ---------------------------------------------------------------------------


def test_iter_records_vscdb_yields_user_and_assistant(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path)
    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))

    recs = list(src.iter_records(session))
    assert len(recs) == 2

    rowids = [rid for rid, _ in recs]
    assert rowids == sorted(rowids)  # monotonically increasing

    _, user = recs[0]
    assert isinstance(user, UserRecord)
    assert user.type == "user"
    assert user.text == "list the relays"
    assert user.session_id == _COMPOSER_ID

    _, asst = recs[1]
    assert isinstance(asst, AssistantRecord)
    assert asst.type == "assistant"
    assert asst.model == "claude-opus-4-7"
    assert asst.usage == {"input_tokens": 10, "output_tokens": 5}
    assert asst.session_id == _COMPOSER_ID


def test_iter_records_vscdb_ts_parsed(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path)
    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))
    recs = list(src.iter_records(session))
    for _, r in recs:
        assert r.ts is not None


def test_iter_records_vscdb_cwd_propagated(tmp_path: Path):
    vscdb = _make_vscdb(
        tmp_path,
        workspace_json={"folder": "file:///home/operator/myproj"},
    )
    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))
    for _, r in src.iter_records(session):
        assert r.cwd == "/home/operator/myproj"


def test_iter_records_vscdb_corrupt_bubble_skipped(tmp_path: Path):
    vscdb = _make_vscdb(
        tmp_path,
        bubbles=[
            {"bubbleId": "good-1", "type": 1, "text": "ok"},
        ],
    )
    # Inject a corrupt (non-JSON) row
    conn = sqlite3.connect(str(vscdb))
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"bubbleId:{_COMPOSER_ID}:bad-row", "not json {{"),
    )
    conn.commit()
    conn.close()

    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))
    recs = list(src.iter_records(session))
    # Only the valid bubble should come through
    assert len(recs) == 1
    assert recs[0][1].text == "ok"


def test_iter_records_vscdb_start_offset_filters(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path)
    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))
    all_recs = list(src.iter_records(session))
    first_rowid = all_recs[0][0]
    resumed = list(src.iter_records(session, start_offset=first_rowid))
    assert len(resumed) == len(all_recs) - 1


def test_iter_records_vscdb_token_usage_snake_case_fallback(tmp_path: Path):
    vscdb = _make_vscdb(
        tmp_path,
        bubbles=[
            {
                "bubbleId": "a1",
                "type": 2,
                "text": "hi",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        ],
    )
    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))
    recs = list(src.iter_records(session))
    assert len(recs) == 1
    _, asst = recs[0]
    assert isinstance(asst, AssistantRecord)
    assert asst.usage == {"input_tokens": 7, "output_tokens": 3}


def test_iter_records_vscdb_timing_ts_fallback(tmp_path: Path):
    """timingInfo.clientRpcSendTime should be used when createdAt absent."""
    vscdb = _make_vscdb(
        tmp_path,
        bubbles=[
            {
                "bubbleId": "u1",
                "type": 1,
                "text": "q",
                "timingInfo": {"clientRpcSendTime": 1716545400000},
            }
        ],
    )
    src = _vscdb_source([vscdb])
    session = next(iter(src.discover_sessions()))
    recs = list(src.iter_records(session))
    assert recs[0][1].ts is not None


# ---------------------------------------------------------------------------
# iter_records — legacy ItemTable path
# ---------------------------------------------------------------------------


def test_iter_records_legacy_item_table(tmp_path: Path):
    vscdb = _make_vscdb(tmp_path, item_table_only=True)
    src = _vscdb_source([vscdb])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.extra["storage"] == "vscdb_legacy"
    recs = list(src.iter_records(s))
    assert len(recs) == 2
    _, user = recs[0]
    assert isinstance(user, UserRecord)
    assert user.text == "hello from legacy"
    _, asst = recs[1]
    assert isinstance(asst, AssistantRecord)
    assert asst.content[0].text == "hi back"


def test_discover_sessions_legacy_no_known_key(tmp_path: Path):
    """A workspace DB without any known ItemTable key → no session yielded."""
    db_dir = tmp_path / "ws"
    db_dir.mkdir()
    vscdb = db_dir / "state.vscdb"
    conn = sqlite3.connect(str(vscdb))
    conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    conn.execute("INSERT INTO ItemTable VALUES ('unrelated.key', 'stuff')")
    conn.commit()
    conn.close()
    src = _vscdb_source([vscdb])
    sessions = list(src.discover_sessions())
    assert sessions == []


# ---------------------------------------------------------------------------
# _extract_bubble_text
# ---------------------------------------------------------------------------


def test_extract_bubble_text_assistant_text_field():
    data = {"type": 2, "text": "hello world"}
    assert _extract_bubble_text(data) == "hello world"


def test_extract_bubble_text_user_content_field():
    data = {"type": 1, "content": "user prompt"}
    assert _extract_bubble_text(data) == "user prompt"


def test_extract_bubble_text_code_blocks():
    data = {
        "type": 1,
        "codeBlocks": [{"content": "print('hi')"}, {"content": "x=1"}],
    }
    result = _extract_bubble_text(data)
    assert "print('hi')" in result
    assert "x=1" in result


def test_extract_bubble_text_assistant_combines_text_and_code():
    data = {
        "type": 2,
        "text": "here is code:",
        "codeBlocks": [{"content": "print('hello')"}],
    }
    result = _extract_bubble_text(data)
    assert "here is code:" in result
    assert "print('hello')" in result


def test_extract_bubble_text_empty_bubble():
    assert _extract_bubble_text({}) is None


# ---------------------------------------------------------------------------
# _bubble_to_record — direct unit tests
# ---------------------------------------------------------------------------


def _fake_vscdb_session(cwd: str | None = None) -> SessionFile:
    return SessionFile(
        source="cursor",
        path=Path("/tmp/fake.vscdb"),
        cwd=cwd,
        session_id=_COMPOSER_ID,
        started_at=None,
        last_modified=0.0,
        extra={"storage": "vscdb", "composer_id": _COMPOSER_ID},
    )


def test_bubble_to_record_user():
    sess = _fake_vscdb_session()
    data = {"type": 1, "bubbleId": "u1", "text": "hello", "createdAt": "2026-05-24T10:00:00Z"}
    rec = _bubble_to_record(data, _COMPOSER_ID, sess, "bubbleId:c:u1")
    assert isinstance(rec, UserRecord)
    assert rec.type == "user"
    assert rec.text == "hello"
    assert rec.ts is not None
    assert rec.session_id == _COMPOSER_ID


def test_bubble_to_record_assistant():
    sess = _fake_vscdb_session()
    data = {
        "type": 2,
        "bubbleId": "a1",
        "text": "response text",
        "modelInfo": {"modelName": "claude-opus-4-7"},
        "tokenCount": {"inputTokens": 20, "outputTokens": 10},
    }
    rec = _bubble_to_record(data, _COMPOSER_ID, sess, "bubbleId:c:a1")
    assert isinstance(rec, AssistantRecord)
    assert rec.type == "assistant"
    assert rec.model == "claude-opus-4-7"
    assert rec.usage == {"input_tokens": 20, "output_tokens": 10}
    assert len(rec.content) == 1
    assert rec.content[0].type == "text"
    assert rec.content[0].text == "response text"


def test_bubble_to_record_cwd_propagated():
    sess = _fake_vscdb_session(cwd="/home/operator/relay")
    data = {"type": 1, "bubbleId": "u1", "text": "q"}
    rec = _bubble_to_record(data, _COMPOSER_ID, sess, "bubbleId:c:u1")
    assert rec.cwd == "/home/operator/relay"


def test_bubble_to_record_raw_preserved():
    sess = _fake_vscdb_session()
    data = {"type": 2, "bubbleId": "a1", "text": "hi", "extra_field": "preserved"}
    rec = _bubble_to_record(data, _COMPOSER_ID, sess, "bubbleId:c:a1")
    assert rec.raw["extra_field"] == "preserved"


def test_bubble_to_record_key_fallback_bubble_id():
    """When bubbleId absent in data, fall back to parsing the row key."""
    sess = _fake_vscdb_session()
    data = {"type": 1, "text": "hi"}
    rec = _bubble_to_record(data, _COMPOSER_ID, sess, "bubbleId:comp:fallback-uuid")
    assert rec.uuid == "fallback-uuid"


# ---------------------------------------------------------------------------
# JSONL + vscdb coexistence — discover_sessions yields both
# ---------------------------------------------------------------------------


def test_jsonl_and_vscdb_coexist(tmp_path: Path):
    # Build a JSONL tree
    _make_cursor_home(
        tmp_path,
        proj_hash="proj1",
        session_name="sess1",
        lines=[{"id": "1", "role": "user", "content": "hi"}],
    )
    # Build a vscdb
    vscdb = _make_vscdb(tmp_path / "vscdb_data")
    src = CursorSource(cursor_home=tmp_path, vscdb_paths=[vscdb])
    sessions = list(src.discover_sessions())
    storages = [s.extra.get("storage", "jsonl") for s in sessions]
    assert "jsonl" in storages or any(s.extra.get("projHash") for s in sessions)
    assert any(s.extra.get("storage") == "vscdb" for s in sessions)
    assert len(sessions) == 2  # 1 JSONL + 1 vscdb composer
