"""Scoped ingest indexes one transcript and advances only that watermark."""

from __future__ import annotations

from pathlib import Path

from index.db import connect
from index.ingest import guess_transcript_source, ingest_targeted


def _pi_line(text: str, entry_id: str) -> str:
    import json

    return (
        json.dumps(
            {
                "type": "message",
                "id": entry_id,
                "parentId": None,
                "timestamp": "2026-10-19T00:00:00Z",
                "message": {"role": "user", "content": text},
            }
        )
        + "\n"
    )


def test_guess_source_from_path(tmp_path: Path) -> None:
    assert guess_transcript_source(tmp_path / ".codex" / "sessions" / "rollout-1.jsonl") == "codex"
    assert guess_transcript_source(Path("/home/a/.pi/agent/sessions/s.jsonl")) == "pi"
    assert guess_transcript_source(Path("/home/a/.claude/projects/p/s.jsonl")) == "claude_code"
    goose_db = Path("/home/a/.local/share/goose/sessions/sessions.db")
    assert guess_transcript_source(goose_db) == "goose"


def test_targeted_pi_ingests_one_file_and_is_idempotent(tmp_path: Path) -> None:
    keep = tmp_path / "keep.jsonl"
    other = tmp_path / "other.jsonl"
    keep.write_text(_pi_line("fix the parser", "m1"), encoding="utf-8")
    other.write_text(_pi_line("do not index me", "m9"), encoding="utf-8")
    conn = connect(tmp_path / "index.db")
    try:
        first = ingest_targeted(conn, path=keep, source="pi")
        assert first.errors == 0
        assert first.new_messages == 1
        second = ingest_targeted(conn, path=keep, source="pi")
        assert second.new_messages == 0
        rows = conn.execute("SELECT text FROM messages").fetchall()
        assert [row[0] for row in rows] == ["fix the parser"]
        keep.write_text(
            keep.read_text(encoding="utf-8") + _pi_line("second turn", "m2"),
            encoding="utf-8",
        )
        third = ingest_targeted(conn, path=keep, source="pi")
        assert third.new_messages == 1
        texts = [row[0] for row in conn.execute("SELECT text FROM messages ORDER BY id")]
        assert texts == ["fix the parser", "second turn"]
        assert "do not index me" not in texts
    finally:
        conn.close()


def test_targeted_missing_file_is_an_error_report(tmp_path: Path) -> None:
    conn = connect(tmp_path / "index.db")
    try:
        report = ingest_targeted(conn, path=tmp_path / "nope.jsonl", source="pi")
        assert report.errors == 1
        assert report.new_messages == 0
    finally:
        conn.close()
