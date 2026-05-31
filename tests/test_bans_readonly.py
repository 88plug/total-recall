"""bans read paths must work on a read-only connection.

The MCP server opens the index `mode=ro`. Before the fix, `check_banned` and
`list_failed_attempts` called `ensure_schema()` (CREATE TABLE) on every call →
`attempt to write a readonly database`. The error was swallowed and a misleading
"not banned" / empty result returned. Read paths now use `_table_exists` and
never write.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from index.bans import (
    check_banned,
    ensure_schema,
    list_failed_attempts,
    upsert_ban,
    upsert_failed_attempt,
)
from index.db import connect


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def test_check_banned_readonly_with_data(tmp_path: Path) -> None:
    """A real ban written via a writable conn is readable via a read-only one."""
    db = tmp_path / "index.db"
    w = connect(db)
    upsert_ban(
        w,
        banned_thing="docker",
        ban_strength="absolute",
        ban_text="never use docker here",
    )
    w.commit()
    w.close()

    ro = _ro(db)
    try:
        hit = check_banned(ro, "docker")
        assert hit is not None
        assert hit["banned_thing"] == "docker"
        # The read must not have written anything (mode=ro would have raised).
        miss = check_banned(ro, "kubernetes")
        assert miss is None
    finally:
        ro.close()


def test_check_banned_readonly_no_table(tmp_path: Path) -> None:
    """A read-only conn on a DB without the bans table returns None, not error."""
    db = tmp_path / "index.db"
    # Create a DB with the core schema but never run the bans extractor.
    connect(db).close()
    ro = _ro(db)
    try:
        assert check_banned(ro, "anything") is None
    finally:
        ro.close()


def test_list_failed_attempts_readonly(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    w = connect(db)
    upsert_failed_attempt(
        w,
        attempt="use a global mutex",
        replaced_by="per-key flock",
        reason="contention",
        cwd="/proj/x",
    )
    w.commit()
    w.close()

    ro = _ro(db)
    try:
        rows = list_failed_attempts(ro, topic="mutex")
        assert len(rows) == 1
        assert rows[0]["attempt"] == "use a global mutex"
        # No-table case on a fresh core DB → empty list, no raise.
        db2 = tmp_path / "empty.db"
        connect(db2).close()
        ro2 = _ro(db2)
        try:
            assert list_failed_attempts(ro2) == []
        finally:
            ro2.close()
    finally:
        ro.close()


def test_ensure_schema_still_creates_for_writers(tmp_path: Path) -> None:
    """ensure_schema (writer path) still creates both tables."""
    db = tmp_path / "index.db"
    conn = connect(db)
    try:
        ensure_schema(conn)
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "bans" in names
        assert "failed_attempts" in names
    finally:
        conn.close()
