"""Tests for :mod:`lib.sources.goose` — the Goose (block/goose) adapter.

Goose stores all session transcripts in a single SQLite DB at
``~/.local/share/goose/sessions/sessions.db`` (overridable via
``$GOOSE_SESSIONS_DB`` / ``$GOOSE_DATA_DIR``). Two tables matter:

* ``sessions`` — one row per session. ``id`` is the session id (e.g.
  ``20260607_3``), ``working_dir`` is the cwd ground truth, ``archived_at``
  marks soft-deleted sessions.
* ``messages`` — append-only turns. ``role`` is ``user`` / ``assistant``,
  ``content_json`` is a JSON *array* of content blocks discriminated by
  ``type`` (``text`` / ``thinking`` / ``toolRequest`` / ``toolResponse``),
  ``created_timestamp`` is **epoch seconds**.

Hermetic: every test builds a synthetic ``sessions.db`` under ``tmp_path``.
The real ``~/.local/share/goose`` is never touched.

Realistic shapes are copied verbatim from a live Goose DB:

* tool call::

    {"type":"toolRequest","id":"call-...","toolCall":{"status":"success",
     "value":{"name":"shell","arguments":{"command":"..."}}},
     "_meta":{"goose_extension":"developer"}}

* tool result::

    {"type":"toolResponse","id":"call-...","toolResult":{"status":"success",
     "value":{"content":[{"type":"text","text":"..."}],"isError":false}}}

* thinking + text (one assistant turn)::

    [{"type":"thinking","thinking":"...","signature":""},
     {"type":"text","text":"..."}]
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.schema import AssistantRecord, UserRecord
from lib.sources.base import SessionFile
from lib.sources.goose import (
    GooseSource,
    _goose_msg_to_record,
    _ts_from_epoch_s,
)

# ---------------------------------------------------------------------------
# Synthetic DB builder — mirrors the live Goose schema exactly
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    user_set_name BOOLEAN DEFAULT FALSE,
    session_type TEXT NOT NULL DEFAULT 'user',
    working_dir TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    extension_data TEXT DEFAULT '{}',
    total_tokens INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    accumulated_total_tokens INTEGER,
    accumulated_input_tokens INTEGER,
    accumulated_output_tokens INTEGER,
    accumulated_cost REAL,
    schedule_id TEXT,
    recipe_json TEXT,
    user_recipe_values_json TEXT,
    provider_name TEXT,
    model_config_json TEXT,
    goose_mode TEXT NOT NULL DEFAULT 'auto',
    archived_at TIMESTAMP,
    project_id TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_timestamp INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tokens INTEGER,
    metadata_json TEXT
);
CREATE INDEX idx_messages_session ON messages(session_id);
"""

# epoch seconds — Goose stores `created_timestamp` in *seconds*.
_TS_USER = 1_780_827_104  # 2026-06-07 10:11:44 UTC
_TS_ASST = 1_780_827_164  # 2026-06-07 10:12:44 UTC

_TOOL_CALL_ID = "call-cefde2dc-d7d6-4bec-aaed-654c06677eca-0"


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    working_dir: str,
    *,
    session_type: str = "user",
    archived_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, name, working_dir, session_type, archived_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, "test session", working_dir, session_type, archived_at),
    )


def _insert_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: list[dict],
    ts: int,
    *,
    message_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(message_id, session_id, role, content_json, created_timestamp, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            message_id,
            session_id,
            role,
            json.dumps(content),
            ts,
            json.dumps({"userVisible": True, "agentVisible": True}),
        ),
    )


def _make_db(
    db_path: Path,
    session_id: str = "20260607_3",
    working_dir: str = "/home/operator/goose-proj",
) -> None:
    """Build a synthetic Goose ``sessions.db`` with one populated session:

    user (text) → assistant (thinking + text) → assistant (toolRequest)
    → user (toolResponse).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA)
        _insert_session(conn, session_id, working_dir)

        _insert_message(
            conn,
            session_id,
            "user",
            [{"type": "text", "text": "grep the xai providers"}],
            _TS_USER,
            message_id="msg_user_1",
        )
        _insert_message(
            conn,
            session_id,
            "assistant",
            [
                {"type": "thinking", "thinking": "the user wants a grep", "signature": ""},
                {"type": "text", "text": "Running the search now."},
            ],
            _TS_ASST,
            message_id="msg_asst_1",
        )
        _insert_message(
            conn,
            session_id,
            "assistant",
            [
                {
                    "type": "toolRequest",
                    "id": _TOOL_CALL_ID,
                    "toolCall": {
                        "status": "success",
                        "value": {
                            "name": "shell",
                            "arguments": {"command": 'grep -r "xai" -l'},
                        },
                    },
                    "_meta": {"goose_extension": "developer"},
                }
            ],
            _TS_ASST,
            message_id="msg_asst_2",
        )
        _insert_message(
            conn,
            session_id,
            "user",
            [
                {
                    "type": "toolResponse",
                    "id": _TOOL_CALL_ID,
                    "toolResult": {
                        "status": "success",
                        "value": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "src/providers/xai.rs\nsrc/providers/mod.rs",
                                }
                            ],
                            "structuredContent": {"exit_code": 0},
                            "isError": False,
                        },
                    },
                }
            ],
            _TS_ASST,
            message_id="msg_user_2",
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def goose_db(tmp_path: Path) -> Path:
    """A populated synthetic Goose ``sessions.db``; returns the data dir."""
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    _make_db(data_dir / "sessions" / "sessions.db")
    return data_dir


def _source(data_dir: Path) -> GooseSource:
    return GooseSource(data_dir=data_dir)


# ---------------------------------------------------------------------------
# Helper unit tests (no IO)
# ---------------------------------------------------------------------------


def test_ts_from_epoch_s_seconds_not_ms() -> None:
    """Goose timestamps are epoch *seconds* — must not be divided by 1000."""
    dt = _ts_from_epoch_s(_TS_USER)
    assert dt == datetime(2026, 6, 7, 10, 11, 44, tzinfo=timezone.utc)
    assert _ts_from_epoch_s(None) is None
    assert _ts_from_epoch_s("bogus") is None


def test_goose_msg_to_record_user_text() -> None:
    sess = SessionFile(
        source="goose",
        path=Path("/tmp/sessions.db"),
        cwd="/tmp/proj",
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    rec = _goose_msg_to_record(
        role="user",
        content=[{"type": "text", "text": "deploy the relay"}],
        ts=_TS_USER,
        message_id="m1",
        session=sess,
    )
    assert isinstance(rec, UserRecord)
    assert rec.type == "user"
    assert rec.text == "deploy the relay"
    assert rec.content_kind == "string"
    assert rec.cwd == "/tmp/proj"
    assert rec.session_id == "s1"
    assert rec.uuid == "m1"
    assert rec.ts == datetime(2026, 6, 7, 10, 11, 44, tzinfo=timezone.utc)


def test_goose_msg_to_record_assistant_thinking_and_text() -> None:
    sess = SessionFile(
        source="goose",
        path=Path("/x"),
        cwd=None,
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    rec = _goose_msg_to_record(
        role="assistant",
        content=[
            {"type": "thinking", "thinking": "reasoning here", "signature": ""},
            {"type": "text", "text": "the answer"},
        ],
        ts=_TS_ASST,
        message_id="m2",
        session=sess,
    )
    assert isinstance(rec, AssistantRecord)
    types = [b.type for b in rec.content]
    assert types == ["thinking", "text"]
    assert rec.content[0].thinking == "reasoning here"
    assert rec.content[1].text == "the answer"


def test_goose_msg_to_record_tool_request_becomes_tool_use() -> None:
    """A toolRequest block maps onto an assistant ``tool_use`` block."""
    sess = SessionFile(
        source="goose",
        path=Path("/x"),
        cwd=None,
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    rec = _goose_msg_to_record(
        role="assistant",
        content=[
            {
                "type": "toolRequest",
                "id": _TOOL_CALL_ID,
                "toolCall": {
                    "status": "success",
                    "value": {"name": "shell", "arguments": {"command": "ls"}},
                },
            }
        ],
        ts=_TS_ASST,
        message_id="m3",
        session=sess,
    )
    assert isinstance(rec, AssistantRecord)
    assert len(rec.content) == 1
    blk = rec.content[0]
    assert blk.type == "tool_use"
    assert blk.tool_use is not None
    assert blk.tool_use.id == _TOOL_CALL_ID
    assert blk.tool_use.name == "shell"
    assert blk.tool_use.input == {"command": "ls"}


def test_goose_msg_to_record_tool_response_becomes_tool_result() -> None:
    """A toolResponse block on a user row maps onto a ``tool_result``."""
    sess = SessionFile(
        source="goose",
        path=Path("/x"),
        cwd=None,
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    rec = _goose_msg_to_record(
        role="user",
        content=[
            {
                "type": "toolResponse",
                "id": _TOOL_CALL_ID,
                "toolResult": {
                    "status": "success",
                    "value": {
                        "content": [{"type": "text", "text": "file_a\nfile_b"}],
                        "isError": False,
                    },
                },
            }
        ],
        ts=_TS_ASST,
        message_id="m4",
        session=sess,
    )
    assert isinstance(rec, UserRecord)
    assert rec.content_kind == "tool_result"
    assert len(rec.tool_results) == 1
    tr = rec.tool_results[0]
    assert tr.tool_use_id == _TOOL_CALL_ID
    assert tr.is_error is False
    assert "file_a" in tr.content


def test_goose_msg_to_record_error_tool_response() -> None:
    sess = SessionFile(
        source="goose",
        path=Path("/x"),
        cwd=None,
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    rec = _goose_msg_to_record(
        role="user",
        content=[
            {
                "type": "toolResponse",
                "id": "call-err",
                "toolResult": {
                    "status": "error",
                    "value": {
                        "content": [{"type": "text", "text": "boom"}],
                        "isError": True,
                    },
                },
            }
        ],
        ts=_TS_ASST,
        message_id="m5",
        session=sess,
    )
    assert isinstance(rec, UserRecord)
    assert rec.tool_results[0].is_error is True


def test_goose_msg_to_record_empty_content() -> None:
    """An empty content array must not raise; yields an empty user record."""
    sess = SessionFile(
        source="goose",
        path=Path("/x"),
        cwd=None,
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    rec = _goose_msg_to_record(
        role="user",
        content=[],
        ts=_TS_USER,
        message_id="m6",
        session=sess,
    )
    assert isinstance(rec, UserRecord)
    assert rec.content_kind == "empty"
    assert rec.text in (None, "")


def test_goose_msg_to_record_raw_preserved() -> None:
    sess = SessionFile(
        source="goose",
        path=Path("/x"),
        cwd=None,
        session_id="s1",
        started_at=None,
        last_modified=0.0,
    )
    content = [{"type": "text", "text": "hi", "extra": "kept"}]
    rec = _goose_msg_to_record(
        role="user",
        content=content,
        ts=_TS_USER,
        message_id="m7",
        session=sess,
    )
    # The original content array round-trips through raw.
    assert rec.raw["content"] == content


# ---------------------------------------------------------------------------
# Adapter wiring
# ---------------------------------------------------------------------------


def test_registered_in_sources() -> None:
    import lib.sources  # noqa: F401 — trigger registration of bundled adapters
    import lib.sources.goose  # noqa: F401
    from lib.sources.base import SOURCES

    assert "goose" in [cls.name for cls in SOURCES]


def test_class_name_constant() -> None:
    assert GooseSource.name == "goose"


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


def test_is_available_false_when_no_data_dir(tmp_path: Path) -> None:
    src = _source(tmp_path / "nope")
    assert src.is_available() is False


def test_is_available_false_when_db_missing(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    src = _source(tmp_path)
    assert src.is_available() is False


def test_is_available_true_when_db_present(goose_db: Path) -> None:
    src = _source(goose_db)
    assert src.is_available() is True


def test_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    _make_db(data_dir / "sessions" / "sessions.db", session_id="env_sess")
    monkeypatch.setenv("GOOSE_DATA_DIR", str(data_dir))

    src = GooseSource()  # no explicit data_dir → must read env
    assert src.is_available() is True
    assert [s.session_id for s in src.discover_sessions()] == ["env_sess"]


# ---------------------------------------------------------------------------
# discover_sessions
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_session_with_cwd(goose_db: Path) -> None:
    src = _source(goose_db)
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert isinstance(s, SessionFile)
    assert s.source == "goose"
    assert s.session_id == "20260607_3"
    assert s.cwd == "/home/operator/goose-proj"  # from sessions.working_dir
    assert s.last_modified > 0


def test_discover_sessions_empty_when_unavailable(tmp_path: Path) -> None:
    assert list(_source(tmp_path / "absent").discover_sessions()) == []


def test_discover_sessions_skips_empty_sessions(tmp_path: Path) -> None:
    """A session row with no messages should not be yielded."""
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    db = data_dir / "sessions" / "sessions.db"
    _make_db(db, session_id="has_msgs")
    conn = sqlite3.connect(str(db))
    _insert_session(conn, "empty_sess", "/home/operator/empty")
    conn.commit()
    conn.close()

    src = _source(data_dir)
    ids = [s.session_id for s in src.discover_sessions()]
    assert "has_msgs" in ids
    assert "empty_sess" not in ids


def test_discover_sessions_skips_archived(tmp_path: Path) -> None:
    """Archived sessions (``archived_at`` set) are skipped by default.

    Rationale: Goose archives are the user's explicit soft-delete; mining
    them would resurface intentionally retired context. They can still be
    opted in via ``include_archived=True``.
    """
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    db = data_dir / "sessions" / "sessions.db"
    _make_db(db, session_id="active")
    conn = sqlite3.connect(str(db))
    _insert_session(conn, "archived", "/home/operator/old", archived_at="2026-06-01 00:00:00")
    _insert_message(conn, "archived", "user", [{"type": "text", "text": "old"}], _TS_USER)
    conn.commit()
    conn.close()

    src = _source(data_dir)
    ids = [s.session_id for s in src.discover_sessions()]
    assert "active" in ids
    assert "archived" not in ids

    src_all = GooseSource(data_dir=data_dir, include_archived=True)
    ids_all = [s.session_id for s in src_all.discover_sessions()]
    assert "archived" in ids_all


def test_discover_sessions_sort_order_stable(tmp_path: Path) -> None:
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    db = data_dir / "sessions" / "sessions.db"
    _make_db(db, session_id="bbb")
    conn = sqlite3.connect(str(db))
    for sid in ("ccc", "aaa"):
        _insert_session(conn, sid, f"/home/operator/{sid}")
        _insert_message(conn, sid, "user", [{"type": "text", "text": "x"}], _TS_USER)
    conn.commit()
    conn.close()

    src = _source(data_dir)
    ids = [s.session_id for s in src.discover_sessions()]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# iter_records
# ---------------------------------------------------------------------------


def test_iter_records_roles_content_and_timestamps(goose_db: Path) -> None:
    src = _source(goose_db)
    session = next(iter(src.discover_sessions()))
    recs = list(src.iter_records(session))
    assert len(recs) == 4

    # Offsets monotonically increasing.
    offsets = [off for off, _ in recs]
    assert offsets == sorted(offsets)
    assert all(a < b for a, b in zip(offsets, offsets[1:], strict=False))

    _, user = recs[0]
    assert isinstance(user, UserRecord)
    assert user.type == "user"
    assert user.text == "grep the xai providers"
    assert user.session_id == "20260607_3"
    assert user.cwd == "/home/operator/goose-proj"
    assert user.ts == datetime(2026, 6, 7, 10, 11, 44, tzinfo=timezone.utc)

    _, asst = recs[1]
    assert isinstance(asst, AssistantRecord)
    assert [b.type for b in asst.content] == ["thinking", "text"]
    assert asst.content[1].text == "Running the search now."
    assert asst.ts == datetime(2026, 6, 7, 10, 12, 44, tzinfo=timezone.utc)

    _, tool_call = recs[2]
    assert isinstance(tool_call, AssistantRecord)
    assert tool_call.content[0].type == "tool_use"
    assert tool_call.content[0].tool_use.name == "shell"

    _, tool_resp = recs[3]
    assert isinstance(tool_resp, UserRecord)
    assert tool_resp.content_kind == "tool_result"
    assert tool_resp.tool_results[0].tool_use_id == _TOOL_CALL_ID


def test_iter_records_start_offset_resumes(goose_db: Path) -> None:
    src = _source(goose_db)
    session = next(iter(src.discover_sessions()))
    all_recs = list(src.iter_records(session))
    first_offset = all_recs[0][0]
    resumed = list(src.iter_records(session, start_offset=first_offset))
    assert [r.uuid for _, r in resumed] == [r.uuid for _, r in all_recs[1:]]


def test_iter_records_session_id_propagates(goose_db: Path) -> None:
    src = _source(goose_db)
    session = next(iter(src.discover_sessions()))
    for _, r in src.iter_records(session):
        assert r.session_id == session.session_id


def test_iter_records_corrupt_content_json_skipped(tmp_path: Path) -> None:
    """A row whose ``content_json`` is not valid JSON is skipped, not fatal."""
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    db = data_dir / "sessions" / "sessions.db"
    _make_db(db, session_id="s1")
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO messages (session_id, role, content_json, created_timestamp) "
        "VALUES (?, ?, ?, ?)",
        ("s1", "user", "not json {{", _TS_USER),
    )
    conn.commit()
    conn.close()

    src = _source(data_dir)
    session = next(iter(src.discover_sessions()))
    # The four valid rows come through; the corrupt one is dropped.
    recs = list(src.iter_records(session))
    assert len(recs) == 4


def test_iter_records_unreadable_db_does_not_raise(tmp_path: Path) -> None:
    data_dir = tmp_path / "goose"
    (data_dir / "sessions").mkdir(parents=True)
    (data_dir / "sessions" / "sessions.db").write_text("not a sqlite db")
    src = _source(data_dir)
    # Discovery on a corrupt DB must be graceful.
    assert list(src.discover_sessions()) == []
