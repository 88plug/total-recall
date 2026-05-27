"""Tests for :mod:`lib.sources.opencode` — the OpenCode session adapter.

Covers:

* Availability gating (no data dir → ``is_available() is False``).
* SQLite path: synthetic ``opencode.db`` with one session, two messages
  (one user, one assistant with text+reasoning+tool parts) →
  :meth:`discover_sessions` yields it and :meth:`iter_records` returns
  correctly typed :class:`lib.schema.Record` instances with ``cwd``,
  ``ts``, ``model``, and ``usage`` populated.
* Legacy JSON path: synthetic ``storage/session/...`` + ``storage/message/...``
  tree → same expectations.
* Env-var override (``$OPENCODE_DATA_DIR``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.schema import AssistantRecord, UserRecord
from lib.sources.opencode import (
    OpenCodeSource,
    _opencode_to_record,
    _remap_tokens,
    _ts_from_ms,
)


# ---------------------------------------------------------------------------
# Helpers — build a synthetic OpenCode SQLite DB
# ---------------------------------------------------------------------------

_SQLITE_DDL = """
CREATE TABLE SessionTable (
    id TEXT PRIMARY KEY,
    info TEXT
);
CREATE TABLE MessageTable (
    id TEXT PRIMARY KEY,
    sessionID TEXT,
    role TEXT,
    info TEXT
);
CREATE TABLE PartTable (
    id TEXT PRIMARY KEY,
    sessionID TEXT,
    messageID TEXT,
    info TEXT
);
"""


def _make_db(db_path: Path, session_id: str, cwd: str) -> None:
    """Create a minimal OpenCode-shaped SQLite DB at ``db_path``."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SQLITE_DDL)

        # Session row with cwd inside JSON `info` (current OpenCode style).
        conn.execute(
            "INSERT INTO SessionTable (id, info) VALUES (?, ?)",
            (session_id, json.dumps({"directory": cwd})),
        )

        # Two messages — user then assistant. Use lexicographically-sortable
        # IDs because production OpenCode uses ULIDs and we
        # ``ORDER BY id`` in the adapter.
        user_msg_id = "msg_001"
        asst_msg_id = "msg_002"
        # ms-epoch timestamps; OpenCode stores milliseconds.
        user_ts_ms = 1_700_000_000_000
        asst_ts_ms = 1_700_000_001_000

        conn.execute(
            "INSERT INTO MessageTable (id, sessionID, role, info) VALUES (?, ?, ?, ?)",
            (
                user_msg_id,
                session_id,
                "user",
                json.dumps({"time": {"created": user_ts_ms}}),
            ),
        )
        conn.execute(
            "INSERT INTO MessageTable (id, sessionID, role, info) VALUES (?, ?, ?, ?)",
            (
                asst_msg_id,
                session_id,
                "assistant",
                json.dumps(
                    {
                        "time": {"created": asst_ts_ms},
                        "agent": "build",
                        "model": {
                            "providerID": "anthropic",
                            "modelID": "claude-opus-4-7",
                        },
                        "tokens": {
                            "input": 100,
                            "output": 50,
                            "reasoning": 10,
                            "cache": {"read": 20, "write": 5},
                        },
                        "finish": {"reason": "end_turn"},
                    }
                ),
            ),
        )

        # User part: one text block.
        conn.execute(
            "INSERT INTO PartTable (id, sessionID, messageID, info) VALUES (?, ?, ?, ?)",
            (
                "prt_001",
                session_id,
                user_msg_id,
                json.dumps({"type": "text", "text": "deploy the relay"}),
            ),
        )

        # Assistant parts: reasoning, text, tool (in order). IDs sort
        # lex so we get deterministic order out of ``ORDER BY id``.
        conn.execute(
            "INSERT INTO PartTable (id, sessionID, messageID, info) VALUES (?, ?, ?, ?)",
            (
                "prt_010",
                session_id,
                asst_msg_id,
                json.dumps({"type": "reasoning", "text": "thinking about it"}),
            ),
        )
        conn.execute(
            "INSERT INTO PartTable (id, sessionID, messageID, info) VALUES (?, ?, ?, ?)",
            (
                "prt_011",
                session_id,
                asst_msg_id,
                json.dumps({"type": "text", "text": "ok deploying now"}),
            ),
        )
        conn.execute(
            "INSERT INTO PartTable (id, sessionID, messageID, info) VALUES (?, ?, ?, ?)",
            (
                "prt_012",
                session_id,
                asst_msg_id,
                json.dumps(
                    {
                        "type": "tool",
                        "id": "call_1",
                        "name": "Bash",
                        "args": {"command": "echo hi"},
                        "state": {"status": "success", "result": "hi"},
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers — build a synthetic legacy JSON tree
# ---------------------------------------------------------------------------


def _make_legacy_tree(storage_root: Path, session_id: str, cwd: str) -> None:
    """Lay out a legacy ``storage/session/...`` + ``storage/message/...``
    tree under ``storage_root``."""

    project_hash = "abcdef01"
    session_dir = storage_root / "session" / project_hash
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.json").write_text(
        json.dumps({"id": session_id, "directory": cwd})
    )

    msg_dir = storage_root / "message" / session_id
    msg_dir.mkdir(parents=True)

    # Ordering by filename: msg_001 (user) before msg_002 (assistant).
    (msg_dir / "msg_001.json").write_text(
        json.dumps(
            {
                "id": "msg_001",
                "role": "user",
                "info": {"time": {"created": 1_700_000_000_000}},
                "parts": [{"type": "text", "text": "deploy please"}],
            }
        )
    )
    (msg_dir / "msg_002.json").write_text(
        json.dumps(
            {
                "id": "msg_002",
                "role": "assistant",
                "info": {
                    "time": {"created": 1_700_000_001_000},
                    "model": {"modelID": "claude-opus-4-7"},
                    "tokens": {
                        "input": 12,
                        "output": 7,
                        "cache": {"read": 3, "write": 1},
                    },
                },
                "parts": [
                    {"type": "text", "text": "deploying"},
                ],
            }
        )
    )


# ---------------------------------------------------------------------------
# Unit tests on helpers (no IO)
# ---------------------------------------------------------------------------


def test_ts_from_ms_roundtrip() -> None:
    dt = _ts_from_ms(1_700_000_000_000)
    assert dt == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert _ts_from_ms(None) is None
    assert _ts_from_ms("bogus") is None


def test_remap_tokens_full() -> None:
    out = _remap_tokens(
        {
            "input": 1,
            "output": 2,
            "reasoning": 3,
            "cache": {"read": 4, "write": 5},
        }
    )
    assert out == {
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 3,
        "cache_read_tokens": 4,
        "cache_creation_tokens": 5,
    }


def test_remap_tokens_partial_and_none() -> None:
    assert _remap_tokens(None) is None
    assert _remap_tokens({}) is None
    assert _remap_tokens({"input": 5}) == {"input_tokens": 5}


def test_opencode_to_record_assistant_minimal() -> None:
    rec = _opencode_to_record(
        {
            "time": {"created": 1_700_000_000_000},
            "model": {"modelID": "claude-opus-4-7"},
            "tokens": {"input": 1, "output": 2},
        },
        [{"type": "text", "text": "hi"}],
        message_id="m1",
        session_id="s1",
        cwd="/tmp/proj",
        role="assistant",
    )
    assert isinstance(rec, AssistantRecord)
    assert rec.model == "claude-opus-4-7"
    assert rec.cwd == "/tmp/proj"
    assert rec.session_id == "s1"
    assert rec.usage == {"input_tokens": 1, "output_tokens": 2}
    assert len(rec.content) == 1 and rec.content[0].text == "hi"


def test_opencode_to_record_user_text() -> None:
    rec = _opencode_to_record(
        {"time": {"created": 1_700_000_000_000}},
        [{"type": "text", "text": "deploy"}],
        message_id="m1",
        session_id="s1",
        cwd="/tmp/proj",
        role="user",
    )
    assert isinstance(rec, UserRecord)
    assert rec.text == "deploy"
    assert rec.content_kind == "string"


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


def test_is_available_false_when_empty(tmp_path: Path) -> None:
    src = OpenCodeSource(data_dirs=[tmp_path / "missing"])
    assert src.is_available() is False
    assert list(src.discover_sessions()) == []


def test_is_available_true_when_db_present(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _make_db(db, session_id="sess_1", cwd="/tmp/proj")
    src = OpenCodeSource(data_dirs=[tmp_path])
    assert src.is_available() is True


def test_is_available_true_when_legacy_present(tmp_path: Path) -> None:
    _make_legacy_tree(tmp_path / "storage", session_id="sess_1", cwd="/tmp/proj")
    src = OpenCodeSource(data_dirs=[tmp_path])
    assert src.is_available() is True


# ---------------------------------------------------------------------------
# SQLite path: discover + iter_records
# ---------------------------------------------------------------------------


def test_sqlite_discover_and_iter(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _make_db(db, session_id="sess_1", cwd="/tmp/proj")

    src = OpenCodeSource(data_dirs=[tmp_path])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source == "opencode"
    assert s.session_id == "sess_1"
    assert s.cwd == "/tmp/proj"
    assert s.extra["storage"] == "sqlite"

    records = [rec for _, rec in src.iter_records(s)]
    assert len(records) == 2

    user, asst = records
    assert isinstance(user, UserRecord)
    assert user.text == "deploy the relay"
    assert user.session_id == "sess_1"
    assert user.cwd == "/tmp/proj"
    assert user.ts == datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)

    assert isinstance(asst, AssistantRecord)
    assert asst.model == "claude-opus-4-7"
    assert asst.usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 10,
        "cache_read_tokens": 20,
        "cache_creation_tokens": 5,
    }
    assert asst.cwd == "/tmp/proj"
    assert asst.ts == datetime.fromtimestamp(1_700_000_001.0, tz=timezone.utc)
    assert asst.stop_reason == "end_turn"

    # Three content blocks: thinking (from "reasoning"), text, tool_use.
    types = [b.type for b in asst.content]
    assert types == ["thinking", "text", "tool_use"]
    assert asst.content[0].thinking == "thinking about it"
    assert asst.content[1].text == "ok deploying now"
    tu = asst.content[2].tool_use
    assert tu is not None
    assert tu.name == "Bash"
    assert tu.input == {"command": "echo hi"}


def test_sqlite_iter_with_start_offset(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _make_db(db, session_id="sess_1", cwd="/tmp/proj")
    src = OpenCodeSource(data_dirs=[tmp_path])
    (s,) = list(src.discover_sessions())
    # Skipping the first record should yield exactly the assistant row.
    recs = list(src.iter_records(s, start_offset=1))
    assert len(recs) == 1
    assert isinstance(recs[0][1], AssistantRecord)


# ---------------------------------------------------------------------------
# Legacy JSON path: discover + iter_records
# ---------------------------------------------------------------------------


def test_legacy_discover_and_iter(tmp_path: Path) -> None:
    _make_legacy_tree(tmp_path / "storage", session_id="sess_legacy", cwd="/tmp/legacy")

    src = OpenCodeSource(data_dirs=[tmp_path])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    s = sessions[0]
    assert s.source == "opencode"
    assert s.session_id == "sess_legacy"
    assert s.cwd == "/tmp/legacy"
    assert s.extra["storage"] == "legacy"
    assert s.extra["project_hash"] == "abcdef01"

    records = [rec for _, rec in src.iter_records(s)]
    assert len(records) == 2
    user, asst = records
    assert isinstance(user, UserRecord)
    assert user.text == "deploy please"
    assert isinstance(asst, AssistantRecord)
    assert asst.model == "claude-opus-4-7"
    assert asst.usage == {
        "input_tokens": 12,
        "output_tokens": 7,
        "cache_read_tokens": 3,
        "cache_creation_tokens": 1,
    }
    assert asst.cwd == "/tmp/legacy"
    # parts: just the text block.
    assert [b.type for b in asst.content] == ["text"]


# ---------------------------------------------------------------------------
# Env-var override
# ---------------------------------------------------------------------------


def test_env_var_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "opencode.db"
    _make_db(db, session_id="sess_env", cwd="/tmp/env")
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))

    src = OpenCodeSource()  # no explicit data_dirs → must read env
    assert src.is_available() is True
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    assert sessions[0].session_id == "sess_env"


def test_env_var_multiple_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _make_db(a / "opencode.db", session_id="sess_a", cwd="/tmp/a")
    _make_legacy_tree(b / "storage", session_id="sess_b", cwd="/tmp/b")
    monkeypatch.setenv("OPENCODE_DATA_DIR", f"{a},{b}")

    src = OpenCodeSource()
    assert src.is_available() is True
    session_ids = {s.session_id for s in src.discover_sessions()}
    assert session_ids == {"sess_a", "sess_b"}


# ---------------------------------------------------------------------------
# Registration sanity (only meaningful if base module exists)
# ---------------------------------------------------------------------------


def test_registered_in_sources_list_if_base_present() -> None:
    try:
        from lib.sources.base import SOURCES  # type: ignore[import-not-found]
    except Exception:
        pytest.skip("lib.sources.base not yet built by XW1")
    assert OpenCodeSource in SOURCES, (
        "OpenCodeSource should register itself on import"
    )
