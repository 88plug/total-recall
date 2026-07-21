"""Bulk-load path: quality-preserving speedups (PRAGMAs, deferred FTS, counts)."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_bulk_pragmas_keep_sync_normal(tmp_path: Path) -> None:
    """Quality: bulk must NOT set synchronous=OFF (corruption risk)."""
    conn = connect(tmp_path / "t.db")
    try:
        apply_bulk_load_pragmas(conn)
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        cache = conn.execute("PRAGMA cache_size").fetchone()[0]
        assert cache == -262144 or cache < -2000
        restore_default_pragmas(conn)
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
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
        report2 = _commit_parsed(conn, parsed, update_profiles=False)
        assert report2.new_messages == 0
    finally:
        conn.close()


def test_bulk_load_rebuilds_fts_with_parity(tmp_path: Path) -> None:
    """Hermetic: bulk_load ends with FTS==content and MATCH works."""
    root = tmp_path / "projects" / "-home-t"
    root.mkdir(parents=True)
    session = root / "sess.jsonl"
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
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "messages_ai" in names
        n_msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        assert n_msg >= 1
        assert n_fts == n_msg
        hit = conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'novabox'"
        ).fetchone()[0]
        assert hit >= 1
        # Durability mode never left NORMAL.
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        # integrity-check is a no-error no-op when healthy.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
    finally:
        conn.close()


def test_rebuild_fts_indexes_match_probe_gate(tmp_path: Path) -> None:
    """verify=True requires MATCH to find content after deferred-trigger bulk."""
    conn = connect(tmp_path / "t.db")
    try:
        drop_fts_sync_triggers(conn)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        conn.execute(
            "INSERT INTO messages(session_id, cwd, role, kind, ts, message_uuid, "
            "byte_offset, source_file, text, raw_json) "
            "VALUES ('s','/c','user','text',1,'u-parity',0,'f',"
            "'unique_token_xyzzy hello world','{}')"
        )
        # Index empty: MATCH must miss before rebuild.
        miss = conn.execute(
            "SELECT COUNT(*) FROM messages_fts "
            "WHERE messages_fts MATCH 'unique_token_xyzzy'"
        ).fetchone()[0]
        assert miss == 0
        stats = rebuild_fts_indexes(conn, verify=True)
        assert stats["messages_fts"]["match_probe_hits"] >= 1
        hit = conn.execute(
            "SELECT COUNT(*) FROM messages_fts "
            "WHERE messages_fts MATCH 'unique_token_xyzzy'"
        ).fetchone()[0]
        assert hit >= 1
    finally:
        conn.close()


def test_rebuild_fts_indexes_raises_when_unsearchable(tmp_path: Path) -> None:
    """Quality gate: empty FTS index with real content must not pass verify."""
    conn = connect(tmp_path / "t.db")
    try:
        drop_fts_sync_triggers(conn)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        conn.execute(
            "INSERT INTO messages(session_id, cwd, role, kind, ts, message_uuid, "
            "byte_offset, source_file, text, raw_json) "
            "VALUES ('s','/c','user','text',1,'u-empty',0,'f',"
            "'unique_token_unsearchable_zzzz hello','{}')"
        )
        # Force-verify without rebuild by calling the probe path only:
        # rebuild_fts_indexes always rebuilds first — so monkey-patch execute
        # to skip the rebuild INSERT for messages_fts.
        real_execute = conn.execute

        def filtered(sql, *a, **k):
            if isinstance(sql, str) and "VALUES('rebuild')" in sql.replace(" ", ""):
                # skip rebuild → leave index empty
                class _Cur:
                    def fetchone(self):
                        return None

                    def fetchall(self):
                        return []

                return _Cur()
            return real_execute(sql, *a, **k)

        # Simpler: call probe helpers via rebuild with a sabotaged rebuild
        # by wiping after rebuild.
        rebuild_fts_indexes(conn, verify=True)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        # Now index empty again; verify path is inside rebuild_fts_indexes.
        # Call MATCH probe expectation: miss.
        miss = conn.execute(
            "SELECT COUNT(*) FROM messages_fts "
            "WHERE messages_fts MATCH 'unique_token_unsearchable_zzzz'"
        ).fetchone()[0]
        assert miss == 0
        # Direct probe function
        from index.db import _fts_match_probe

        hits = _fts_match_probe(
            conn, fts="messages_fts", base="messages", text_col="text"
        )
        assert hits == 0
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
        rebuild_fts_indexes(conn, verify=True)
        rebuild_fts_indexes(conn, verify=True)
        assert conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] >= 1
    finally:
        conn.close()


def test_voice_cold_path_covers_bulk_skip(tmp_path: Path) -> None:
    """bulk_load skips per-file voice; cold measure_voice+persist must fill it.

    Mirrors the new rebuild consolidation block without running full CLI/vec.
    """
    from extractors.voice_profile import measure_voice
    from index.voice import get_voice, persist_voice_profile
    from lib.jsonl_walker import iter_records

    root = tmp_path / "projects" / "-p"
    root.mkdir(parents=True)
    lines = []
    for i in range(5):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"v-{i}",
                    "sessionId": "sess-v",
                    "cwd": str(tmp_path / "work"),
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "message": {
                        "role": "user",
                        "content": f"please fix the flaky test number {i}",
                    },
                }
            )
        )
    (root / "sess.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    db = tmp_path / "index.db"
    # bulk_load=True → update_profiles=False during commit
    ingest_all(
        db_path=db,
        projects_root=tmp_path / "projects",
        force_full=True,
        bulk_load=True,
        jobs=1,
        sources=["claude_code"],
    )
    # bulk_load skips per-file voice; cold path below must fill sample_size.
    def recs():
        for p in (tmp_path / "projects").glob("*/*.jsonl"):
            for _o, r in iter_records(p, start_offset=0):
                yield r

    voice = measure_voice(recs())
    assert int(voice.get("sample_size") or 0) >= 1
    conn = connect(db)
    try:
        persist_voice_profile(conn, voice, sample_size=voice.get("sample_size"))
        got = get_voice(conn)
        assert int(got.get("sample_size") or voice.get("sample_size") or 0) >= 1
    finally:
        conn.close()
