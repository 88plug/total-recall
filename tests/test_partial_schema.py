"""Partial-schema DB must degrade gracefully, not crash with exit 2.

A DB created only by `adaptive` has just `reinjection_outcomes`; the core
`messages`/`extractions` tables are absent. Read-only query commands open the
DB without applying the schema, so they hit "no such table". Before the fix
these surfaced as `internal error` + exit 2. They should now exit 1 with a
clear "index not built" message (handled centrally in __main__.main).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from total_recall.__main__ import main


@pytest.fixture
def partial_db(tmp_path: Path) -> Path:
    """A DB that exists but has only the adaptive table, no core schema."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reinjection_outcomes (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize(
    "argv",
    [
        ["stats"],
        ["dump", "--format", "jsonl", "--limit", "1"],
        ["metrics", "summary"],
        ["metrics", "sessions"],
    ],
)
def test_partial_schema_exits_one_not_two(
    partial_db: Path, argv: list[str], capsys: pytest.CaptureFixture
) -> None:
    rc = main(["--db", str(partial_db), *argv])
    assert rc == 1, f"{argv} should exit 1 on a partial-schema DB, got {rc}"
    err = capsys.readouterr().err
    assert "index has not been built" in err or "no such table" in err
