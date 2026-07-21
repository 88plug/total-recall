"""Bulk-load path: rebuild PRAGMAs, total_changes counts, deferred FTS/profiles."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from index.db import (
    apply_bulk_load_pragmas,
    connect,
    drop_fts_sync_triggers,
    rebuild_fts_indexes,
    recreate_fts_sync_triggers,
    restore_default_pragmas,
)
from index.ingest import _commit_parsed, _ParsedFile, ingest_all


def _msg_row(
    *,
    session_id: str = "s1",
    cwd: str = "/proj",
    text: str = "hello",
    uuid: str = "m1",
    offset: int = 0,
) -> tuple:
    # Matches _row_for_message trailing shape (incl source + project_key).
    return (
        session_id,
        cwd,
        "main",
        "user",
        "text",
        1_700_000_000,
        None,
        uuid,
        offset,
        "/tmp/fake.jsonl",
        text,
        "{}",
        "claude_code",
        cwd,
    )


def test_bulk_pragmas_apply_and_restore(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    try:
        apply_bulk_load_pragmas(conn)
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 0  # OFF
        cache = conn.execute("PRAGMA cache_size").fetchone()[0]
        assert cache == -262144 or cache < -2000
        restore_default_pragmas(conn)
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        conn.close()


def test_drop_and_recreate_fts_triggers(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "messages_ai" in names
        drop_fts_sync_triggers(conn)
        names2 = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "messages_ai" not in names2
        recreate_fts_sync_triggers(conn)
        names3 = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "messages_ai" in names3
    finally:
        conn.close()


def test_pk_range_counts_new_messages(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    try:
        rows = [_msg_row(uuid=f"u{i}", offset=i * 10, text=f"msg {i}") for i in range(3)]
        parsed = _ParsedFile(
            source_file="/tmp/fake.jsonl",
            inode=1,
            size=100,
            mtime=1,
            start_offset=0,
            rotated=False,
            last_session_id="s1",
            message_rows=rows,
            errors=0,
        )
        report = _commit_parsed(conn, parsed, update_profiles=False)
        assert report.new_messages == 3
        # Re-insert same UUIDs → INSERT OR IGNORE → zero new
        report2 = _commit_parsed(conn, parsed, update_profiles=False)
        assert report2.new_messages == 0
    finally:
        conn.close()


def test_bulk_load_skips_profiles_and_rebuilds_fts(tmp_path: Path) -> None:
    """Hermetic corpus: bulk_load finishes with FTS searchable + no crash."""
    root = tmp_path / "projects" / "-home-t"
    root.mkdir(parents=True)
    session = root / "sess.jsonl"
    # Minimal Claude Code-ish user line the parser can ingest.
    line = {
        "type": "user",
        "uuid": "uuid-bulk-1",
        "sessionId": "sess-bulk",
        "cwd": str(tmp_path / "proj"),
        "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"role": "user", "content": "bulk load fts check novabox"},
    }
    session.write_text(json.dumps(line) + "\n", encoding="utf-8")

    db = tmp_path / "index.db"
    reports = ingest_all(
        db_path=db,
        projects_root=tmp_path / "projects",
        force_full=True,
        bulk_load=True,
        jobs=1,
        sources=["claude_code"],
    )
    assert sum(r.errors for r in reports) == 0
    assert sum(r.new_messages for r in reports) >= 1

    conn = connect(db)
    try:
        # Triggers restored after bulk.
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "messages_ai" in names
        # FTS rebuilt from content.
        n_msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        assert n_msg >= 1
        assert n_fts == n_msg
        # Search hits.
        hit = conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'novabox'"
        ).fetchone()[0]
        assert hit >= 1
        # Defaults restored.
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_rebuild_fts_indexes_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    try:
        conn.execute(
            "INSERT INTO messages(session_id, cwd, role, kind, ts, message_uuid, "
            "byte_offset, source_file, text, raw_json) "
            "VALUES ('s','/c','user','text',1,'u',0,'f','hello world','{}')"
        )
        conn.commit() if conn.in_transaction else None
        # isolation_level=None → autocommit; just rebuild twice
        rebuild_fts_indexes(conn)
        rebuild_fts_indexes(conn)
        assert conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] >= 1
    finally:
        conn.close()
