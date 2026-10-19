"""Pi session adapter: message lines are indexed, compaction entries are not."""

from __future__ import annotations

import json
from pathlib import Path

from lib.schema import UserRecord
from lib.sources.base import SessionFile
from lib.sources.pi import PiSource


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def test_iter_records_keeps_user_text_and_skips_compaction(tmp_path: Path) -> None:
    path = tmp_path / "sess.jsonl"
    path.write_text(
        _line(
            {
                "type": "message",
                "id": "m1",
                "parentId": None,
                "timestamp": "2026-10-19T00:00:00Z",
                "message": {"role": "user", "content": "fix the parser"},
            }
        )
        + _line(
            {
                "type": "compaction",
                "id": "c1",
                "parentId": "m1",
                "timestamp": "2026-10-19T00:01:00Z",
                "summary": "SHOULD NOT BE INDEXED",
                "firstKeptEntryId": "m1",
            }
        ),
        encoding="utf-8",
    )
    session = SessionFile(
        source="pi",
        path=path,
        cwd="/proj",
        session_id="sess",
        started_at=None,
        last_modified=path.stat().st_mtime,
    )
    records = [rec for _off, rec in PiSource().iter_records(session)]
    assert len(records) == 1
    assert isinstance(records[0], UserRecord)
    assert records[0].text == "fix the parser"
    assert "SHOULD NOT BE INDEXED" not in (records[0].text or "")


def test_second_pass_resumes_after_appended_line(tmp_path: Path) -> None:
    path = tmp_path / "sess.jsonl"
    first = _line(
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-10-19T00:00:00Z",
            "message": {"role": "user", "content": "first"},
        }
    )
    path.write_text(first, encoding="utf-8")
    src = PiSource()
    session = SessionFile(
        source="pi",
        path=path,
        cwd=None,
        session_id="sess",
        started_at=None,
        last_modified=0,
    )
    first_pass = list(src.iter_records(session))
    assert len(first_pass) == 1
    cursor = first_pass[0][0]
    path.write_text(
        first
        + _line(
            {
                "type": "message",
                "id": "m2",
                "parentId": "m1",
                "timestamp": "2026-10-19T00:02:00Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            }
        ),
        encoding="utf-8",
    )
    second = list(src.iter_records(session, start_offset=cursor))
    assert len(second) == 1
    assert second[0][1].type == "assistant"
