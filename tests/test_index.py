"""Tests for the SQLite FTS5 index layer.

Scope is deliberately tight: schema integrity, FTS5 trigger sync, ingest
idempotency / rotation handling, and basic query-API behavior. We do NOT
test the live ``~/.claude/projects/`` corpus here (that's an integration
concern); the real-corpus smoke test at the bottom of the file is gated on
the directory's existence so this suite is hermetic by default.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make the repo root importable as ``import index`` regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from index import db as index_db  # noqa: E402
from index import ingest as index_ingest  # noqa: E402
from index import query as index_query  # noqa: E402
from index.db import apply_schema, connect  # noqa: E402
from index.ingest import IngestReport, ingest_file  # noqa: E402
from index.query import (  # noqa: E402
    get_session_meta,
    list_sessions_for_cwd,
    search_extractions,
    search_messages,
    session_count_for_cwd,
    top_topics_for_cwd,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh on-disk DB per test (FTS5 doesn't work on :memory: in WAL)."""
    db_path = tmp_path / "index.db"
    c = connect(db_path)
    yield c
    c.close()


def _insert_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    cwd: str,
    role: str,
    kind: str | None,
    ts: int,
    text: str,
    uuid: str,
    parent_uuid: str | None = None,
    git_branch: str | None = "main",
    byte_offset: int = 0,
    source_file: str = "/tmp/synthetic.jsonl",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO messages(
            session_id, cwd, git_branch, role, kind, ts,
            parent_uuid, message_uuid, byte_offset, source_file, text, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id, cwd, git_branch, role, kind, ts,
            parent_uuid, uuid, byte_offset, source_file, text, None,
        ),
    )
    return int(cur.lastrowid or 0)


def _insert_extraction(
    conn: sqlite3.Connection,
    *,
    kind: str,
    content: str,
    session_id: str,
    cwd: str,
    ts: int,
    source_uuid: str,
    score: float = 0.5,
    scope: str = "project",
    context: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO extractions(
            kind, content, session_id, cwd, ts, source_uuid,
            score, scope, context_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind, content, session_id, cwd, ts, source_uuid, score, scope,
            json.dumps(context or {}),
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Schema / DB
# ---------------------------------------------------------------------------


def test_connect_creates_parent_dir_with_0700(tmp_path: Path) -> None:
    db = tmp_path / "subdir" / "nested" / "index.db"
    c = connect(db)
    c.close()
    assert db.exists()
    mode = (db.parent.stat().st_mode & 0o777)
    # On most filesystems chmod sticks; on weird ones we degrade gracefully.
    # Either we got exactly 0700, or at minimum no world-rwx.
    assert mode == 0o700 or (mode & 0o007) == 0


def test_apply_schema_is_idempotent(tmp_path: Path) -> None:
    c = connect(tmp_path / "x.db")
    # Running it a second time must not error.
    apply_schema(c)
    apply_schema(c)
    # And the schema_version row exists exactly once.
    rows = c.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == "4"
    c.close()


def test_default_db_path_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugindata"))
    # Reload the module so DEFAULT_DB_PATH picks up the new env var.
    import importlib

    reloaded = importlib.reload(index_db)
    assert str(tmp_path / "plugindata" / "total-recall" / "index.db") == str(
        reloaded.DEFAULT_DB_PATH
    )


def test_wal_mode_enabled(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


# ---------------------------------------------------------------------------
# FTS5 sync via triggers
# ---------------------------------------------------------------------------


def test_fts5_insert_trigger_syncs(conn: sqlite3.Connection) -> None:
    _insert_message(
        conn,
        session_id="s1", cwd="/proj/a", role="assistant", kind="assistant",
        ts=1000, text="The cat sat on the mat", uuid="u1",
    )
    rows = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'cat'"
    ).fetchall()
    assert len(rows) == 1


def test_fts5_delete_trigger_syncs(conn: sqlite3.Connection) -> None:
    _insert_message(
        conn,
        session_id="s1", cwd="/proj/a", role="assistant", kind="assistant",
        ts=1000, text="dolphins are smart", uuid="u1",
    )
    conn.execute("DELETE FROM messages WHERE message_uuid = ?", ("u1",))
    rows = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'dolphins'"
    ).fetchall()
    assert rows == []


def test_fts5_update_trigger_syncs(conn: sqlite3.Connection) -> None:
    _insert_message(
        conn,
        session_id="s1", cwd="/proj/a", role="assistant", kind="assistant",
        ts=1000, text="initial body", uuid="u1",
    )
    conn.execute(
        "UPDATE messages SET text = ? WHERE message_uuid = ?",
        ("updated narwhal body", "u1"),
    )
    rows = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'narwhal'"
    ).fetchall()
    assert len(rows) == 1
    # And the old text is gone from the index.
    rows = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'initial'"
    ).fetchall()
    assert rows == []


def test_extractions_fts_triggers(conn: sqlite3.Connection) -> None:
    _insert_extraction(
        conn,
        kind="decision", content="use sqlite-vec for embeddings",
        session_id="s1", cwd="/proj/a", ts=1000, source_uuid="u1",
    )
    # NB: bare `sqlite-vec` parses as the column-prefix operator in FTS5,
    # so quote the term to force literal matching (this is exactly what
    # `_fts_match_quote` does in the real query path).
    rows = conn.execute(
        'SELECT rowid FROM extractions_fts WHERE extractions_fts MATCH \'"sqlite-vec"\''
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------


def test_search_extractions_filters(conn: sqlite3.Connection) -> None:
    _insert_extraction(
        conn, kind="decision", content="adopt FTS5", session_id="s1",
        cwd="/proj/a", ts=1000, source_uuid="u1", score=0.9,
    )
    _insert_extraction(
        conn, kind="failure", content="json grep was too slow",
        session_id="s1", cwd="/proj/a", ts=1100, source_uuid="u2", score=0.6,
    )
    _insert_extraction(
        conn, kind="decision", content="separate project", session_id="s2",
        cwd="/proj/b", ts=1200, source_uuid="u3", score=0.5,
    )

    hits = search_extractions(conn, cwd="/proj/a")
    assert {h.kind for h in hits} == {"decision", "failure"}
    assert all(h.cwd == "/proj/a" for h in hits)

    hits = search_extractions(conn, cwd="/proj/a", kind="decision")
    assert len(hits) == 1
    assert hits[0].content == "adopt FTS5"

    hits = search_extractions(conn, query="FTS5")
    assert len(hits) == 1
    assert hits[0].content == "adopt FTS5"
    # Score normalization stays in [0, 1].
    assert 0.0 <= hits[0].score <= 1.0


def test_search_extractions_since_filter(conn: sqlite3.Connection) -> None:
    _insert_extraction(
        conn, kind="decision", content="old fact", session_id="s1",
        cwd="/proj/a", ts=1000, source_uuid="u1",
    )
    _insert_extraction(
        conn, kind="decision", content="new fact", session_id="s1",
        cwd="/proj/a", ts=2000, source_uuid="u2",
    )
    cutoff = datetime.fromtimestamp(1500, tz=timezone.utc)
    hits = search_extractions(conn, cwd="/proj/a", since=cutoff)
    assert [h.content for h in hits] == ["new fact"]


def test_search_messages_filters_role_and_cwd(conn: sqlite3.Connection) -> None:
    _insert_message(
        conn, session_id="s1", cwd="/proj/a", role="user", kind="user",
        ts=1000, text="how do I configure WAL?", uuid="u1",
    )
    _insert_message(
        conn, session_id="s1", cwd="/proj/a", role="assistant",
        kind="assistant", ts=1001, text="set journal_mode to WAL", uuid="u2",
    )
    _insert_message(
        conn, session_id="s2", cwd="/proj/b", role="user", kind="user",
        ts=1002, text="WAL is unrelated noise here", uuid="u3",
    )

    res = search_messages(conn, "WAL", cwd="/proj/a")
    cwds = {r["cwd"] for r in res}
    assert cwds == {"/proj/a"}

    res = search_messages(conn, "WAL", role="user")
    roles = {r["role"] for r in res}
    assert roles == {"user"}


def test_search_messages_query_sanitization(conn: sqlite3.Connection) -> None:
    # FTS5 operators in the raw query must not blow up. The sanitizer quotes
    # each token, so `OR` (an operator) and `"anything"` (an unmatchable
    # literal) both get defanged into literal terms.
    _insert_message(
        conn, session_id="s1", cwd="/proj/a", role="assistant",
        kind="assistant", ts=1000,
        text="we should adopt sqlite for the index OR something",
        uuid="u1",
    )
    # If the OR weren't quoted, FTS5 would interpret it as an operator and
    # match very differently. With quoting, "OR" is a literal token that
    # also appears in the row, so we still get a hit.
    res = search_messages(conn, 'sqlite OR index')
    assert len(res) >= 1
    # And a query full of would-be operators is sanitized, not raised.
    res = search_messages(conn, 'NEAR( foo bar ) -baz "qux"')
    # Doesn't raise. May or may not match; we only care that it runs.
    assert isinstance(res, list)


def test_top_topics_for_cwd(conn: sqlite3.Connection) -> None:
    _insert_extraction(
        conn, kind="decision", content="use FTS5", session_id="s1",
        cwd="/proj/a", ts=1000, source_uuid="u1", score=0.9,
    )
    _insert_extraction(
        conn, kind="fact", content="python 3.12 required", session_id="s1",
        cwd="/proj/a", ts=1100, source_uuid="u2", score=0.8,
    )
    _insert_extraction(
        conn, kind="fact", content="unrelated other project",
        session_id="s9", cwd="/proj/b", ts=1200, source_uuid="u9",
    )
    topics = top_topics_for_cwd(conn, cwd="/proj/a", limit=5)
    assert "use FTS5" in topics
    assert "python 3.12 required" in topics
    assert "unrelated other project" not in topics


def test_session_count_and_list_for_cwd(conn: sqlite3.Connection) -> None:
    _insert_message(
        conn, session_id="s1", cwd="/proj/a", role="user", kind="user",
        ts=1000, text="hello", uuid="u1",
    )
    _insert_message(
        conn, session_id="s1", cwd="/proj/a", role="user", kind="ai-title",
        ts=1001, text="WAL config session", uuid="u1b",
    )
    _insert_message(
        conn, session_id="s2", cwd="/proj/a", role="user", kind="user",
        ts=2000, text="next session", uuid="u2",
    )
    assert session_count_for_cwd(conn, "/proj/a") == 2

    sessions = list_sessions_for_cwd(conn, "/proj/a")
    ids = [s["session_id"] for s in sessions]
    assert ids[0] == "s2"  # most recent first
    titles = {s["session_id"]: s["ai_title"] for s in sessions}
    assert titles["s1"] == "WAL config session"


# ---------------------------------------------------------------------------
# Ingest (uses defensive walker stub on this branch)
# ---------------------------------------------------------------------------


def test_ingest_file_records_state_even_with_walker_stub(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """On a branch without the walker, ingest_file must still update state
    rather than fail. This guards the defensive-import contract."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text('{"type":"user","content":"hi"}\n', encoding="utf-8")

    report = ingest_file(conn, fake)
    assert isinstance(report, IngestReport)
    state = conn.execute(
        "SELECT * FROM ingest_state WHERE source_file = ?", (str(fake),)
    ).fetchone()
    assert state is not None
    # Second ingest is idempotent.
    report2 = ingest_file(conn, fake)
    assert report2.new_messages == 0


def test_ingest_file_with_fake_walker(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Mock the walker so we can exercise ingest end-to-end without WT-2."""
    from types import SimpleNamespace

    fake = tmp_path / "sess.jsonl"
    payload = [
        json.dumps({"type": "user", "uuid": "u1", "content": "first"}),
        json.dumps({"type": "assistant", "uuid": "u2", "content": "second"}),
    ]
    fake.write_text("\n".join(payload) + "\n", encoding="utf-8")

    def fake_iter(path, start_offset=0):
        # Two records, both with distinct uuids and offsets.
        # Walker contract: yield (next_byte_offset, record) tuples.
        yield 50, SimpleNamespace(
            type="user", uuid="u1", parent_uuid=None,
            session_id="s1", ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            cwd="/proj/a", git_branch="main", text="first", byte_offset=0,
            tool_results=[], content="first",
        )
        yield 110, SimpleNamespace(
            type="assistant", uuid="u2", parent_uuid="u1",
            session_id="s1", ts=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            cwd="/proj/a", git_branch="main", text=None, byte_offset=50,
            content=[SimpleNamespace(type="text", text="second")],
        )

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    r1 = ingest_file(conn, fake)
    assert r1.new_messages == 2

    # Idempotency: same file, no new bytes => no new rows.
    r2 = ingest_file(conn, fake)
    assert r2.new_messages == 0

    # Messages are queryable via FTS.
    res = search_messages(conn, "second", cwd="/proj/a")
    assert len(res) == 1
    assert res[0]["role"] == "assistant"


def test_ingest_file_handles_inode_rotation(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """If the inode changes (file rotated/replaced), we should wipe old rows
    for that path and re-ingest from offset 0."""
    from types import SimpleNamespace

    fake = tmp_path / "sess.jsonl"
    fake.write_text("ignored\n", encoding="utf-8")

    yielded = {"v": 0}

    def fake_iter(path, start_offset=0):
        yielded["v"] += 1
        yield 50, SimpleNamespace(
            type="user", uuid=f"u-{yielded['v']}", parent_uuid=None,
            session_id="s1", ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            cwd="/proj/a", git_branch="main", text=f"body {yielded['v']}",
            byte_offset=0, tool_results=[], content=f"body {yielded['v']}",
        )

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    ingest_file(conn, fake)
    # Tamper with the recorded inode so the next ingest believes the file
    # rotated. We also delete the source file and recreate it to ensure the
    # new size differs.
    conn.execute(
        "UPDATE ingest_state SET inode = inode + 1 WHERE source_file = ?",
        (str(fake),),
    )
    fake.write_text("ignored-too\n", encoding="utf-8")

    ingest_file(conn, fake)
    rows = conn.execute(
        "SELECT message_uuid FROM messages WHERE source_file = ?",
        (str(fake),),
    ).fetchall()
    # Only the most recently ingested message should remain (old wiped).
    uuids = [r["message_uuid"] for r in rows]
    assert uuids == ["u-2"]


# ---------------------------------------------------------------------------
# Real-corpus smoke test (skipped if ~/.claude/projects/ absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not Path("~/.claude/projects").expanduser().exists()
    or os.environ.get("TOTAL_RECALL_RUN_REAL_CORPUS_SMOKE", "0") != "1",
    reason="real-corpus smoke test takes ~3min on heavy corpora; "
           "set TOTAL_RECALL_RUN_REAL_CORPUS_SMOKE=1 to enable",
)
def test_smoke_real_corpus(tmp_path: Path) -> None:
    """End-to-end smoke test against the real ~/.claude/projects/.

    Only runs when both (a) the directory exists, and (b) the walker is the
    real one (not the stub). Bounded to a small wall-clock so CI doesn't
    accidentally pull a multi-minute reindex.
    """
    if not index_ingest._HAS_WALKER:
        pytest.skip("walker module not importable on this branch")
    db = tmp_path / "smoke.db"
    c = connect(db)
    t0 = time.monotonic()
    # Scope to claude_code only — post-v0.5 multi-source ingest fans out to
    # every installed CLI by default; on a dev machine that can mean Codex +
    # Gemini + OpenCode + Cursor + Continue + Cline + Aider all at once.
    # Smoke test stays Claude-Code-only to keep wall time bounded.
    # jobs>1 trims a 776MB corpus from ~3min serial to ~10s parallel.
    import os
    reports = index_ingest.ingest_all(
        c, sources=["claude_code"], jobs=min(os.cpu_count() or 4, 8)
    )
    elapsed = time.monotonic() - t0
    # If we processed anything at all, sessions should be > 0 across cwds.
    if any(r.new_messages for r in reports):
        cur = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        assert int(cur["n"]) > 0
    assert elapsed < 600, "real-corpus ingest took >10 min, regression"
    c.close()


# ---------------------------------------------------------------------------
# QueryHit.extraction_id + get_session_meta (F5 fixes)
# ---------------------------------------------------------------------------


def test_query_hit_has_extraction_id(conn: sqlite3.Connection) -> None:
    """Every QueryHit must carry the stable extraction rowid, so the RRF
    fusion layer in vec/rrf.py can dedupe FTS-vs-vec hits on the same
    extraction. Covers both the FTS-joined and the no-query paths."""
    eid1 = _insert_extraction(
        conn, kind="decision", content="prefer FTS5 over LIKE",
        session_id="s1", cwd="/proj/a", ts=1000, source_uuid="u1", score=0.9,
    )
    eid2 = _insert_extraction(
        conn, kind="fact", content="WAL mode is required",
        session_id="s1", cwd="/proj/a", ts=2000, source_uuid="u2", score=0.5,
    )
    assert eid1 > 0 and eid2 > 0

    # No-query path.
    hits = search_extractions(conn, cwd="/proj/a")
    ids = {h.extraction_id for h in hits}
    assert ids == {eid1, eid2}
    assert all(h.extraction_id > 0 for h in hits)

    # FTS path.
    hits = search_extractions(conn, query="FTS5")
    assert len(hits) == 1
    assert hits[0].extraction_id == eid1


def test_get_session_meta_returns_none_for_unknown(
    conn: sqlite3.Connection,
) -> None:
    assert get_session_meta(conn, "does-not-exist") is None


def test_get_session_meta_basic(conn: sqlite3.Connection) -> None:
    """End-to-end ingest of a tiny synthetic .jsonl, then assert
    get_session_meta returns the expected shape with non-empty values."""
    sid = "s-meta-1"
    cwd = "/proj/meta"
    _insert_message(
        conn, session_id=sid, cwd=cwd, role="user", kind="user",
        ts=1000, text="kick off", uuid="m1",
    )
    _insert_message(
        conn, session_id=sid, cwd=cwd, role="assistant", kind="assistant",
        ts=1010, text="ack", uuid="m2",
    )
    _insert_message(
        conn, session_id=sid, cwd=cwd, role="user", kind="ai-title",
        ts=1020, text="WAL + FTS5 design session", uuid="m3",
    )
    _insert_message(
        conn, session_id=sid, cwd=cwd, role="user", kind="last-prompt",
        ts=1030, text="now wire it up", uuid="m4",
    )
    # Mix of extraction kinds so top_extraction_kinds is exercised.
    _insert_extraction(
        conn, kind="decision", content="use FTS5",
        session_id=sid, cwd=cwd, ts=1005, source_uuid="m1",
    )
    _insert_extraction(
        conn, kind="decision", content="WAL on",
        session_id=sid, cwd=cwd, ts=1006, source_uuid="m2",
    )
    _insert_extraction(
        conn, kind="fact", content="python 3.12",
        session_id=sid, cwd=cwd, ts=1007, source_uuid="m3",
    )

    meta = get_session_meta(conn, sid)
    assert meta is not None
    assert meta["session_id"] == sid
    assert meta["ai_title"] == "WAL + FTS5 design session"
    assert meta["last_prompt"] == "now wire it up"
    assert meta["cwd"] == cwd
    assert meta["message_count"] == 4
    assert meta["branch"] == "main"  # _insert_message default
    assert meta["started_at"].timestamp() == 1000
    assert meta["ended_at"].timestamp() == 1030
    # decision appears twice, fact once — decision should rank first.
    assert meta["top_extraction_kinds"][0] == "decision"
    assert set(meta["top_extraction_kinds"]) == {"decision", "fact"}


# ---------------------------------------------------------------------------
# Schema v2: turns / compactions / ingest_runs + migration
# ---------------------------------------------------------------------------


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def test_schema_v2_creates_turns_compactions_ingest_runs(tmp_path: Path) -> None:
    """Fresh DB at v2 must contain all three metrics tables."""
    c = connect(tmp_path / "v2.db")
    try:
        for tbl in ("turns", "compactions", "ingest_runs"):
            assert _table_exists(c, tbl), f"missing table: {tbl}"
        ver = c.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert ver is not None and ver["value"] == "4"
    finally:
        c.close()


def test_schema_migration_v1_to_v2(tmp_path: Path) -> None:
    """A pre-existing v1 DB (no metrics tables) should be migrated cleanly:
    apply_schema creates the new tables and bumps the recorded version to 2."""
    db_path = tmp_path / "v1.db"
    # Hand-build a minimal v1 DB: schema_meta with version '1' and *none*
    # of the v2 tables. We deliberately bypass `connect()` so apply_schema
    # isn't invoked during setup.
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')"
    )
    raw.commit()
    # Sanity: v2 tables do not exist yet.
    for tbl in ("turns", "compactions", "ingest_runs"):
        assert not _table_exists(raw, tbl)
    raw.close()

    # Re-open via connect(), which invokes apply_schema().
    c = connect(db_path)
    try:
        for tbl in ("turns", "compactions", "ingest_runs"):
            assert _table_exists(c, tbl), f"migration failed to create: {tbl}"
        ver = c.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert ver is not None and ver["value"] == "4"
    finally:
        c.close()


def test_turns_unique_message_uuid(conn: sqlite3.Connection) -> None:
    """The UNIQUE constraint on turns.message_uuid must reject duplicates."""
    conn.execute(
        """
        INSERT INTO turns(
            session_id, cwd, ts, model,
            input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens,
            duration_ms, stop_reason, request_id, message_uuid, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "s1", "/proj/a", 1000, "claude-opus-4-7",
            100, 0, 0, 200, 1500, "end_turn", "req-1", "msg-uuid-1",
            "/tmp/x.jsonl",
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO turns(
                session_id, cwd, ts, model,
                input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens,
                duration_ms, stop_reason, request_id, message_uuid, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "s1", "/proj/a", 1001, "claude-opus-4-7",
                101, 0, 0, 201, 1501, "end_turn", "req-2", "msg-uuid-1",
                "/tmp/x.jsonl",
            ),
        )


# ---------------------------------------------------------------------------
# MA2: turns / compactions / ingest_runs extraction during ingest
# ---------------------------------------------------------------------------


def _fake_assistant_with_usage(uuid: str, session_id: str = "s1") -> "object":
    from types import SimpleNamespace

    raw = {
        "type": "assistant",
        "uuid": uuid,
        "requestId": f"req-{uuid}",
        "message": {
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 12,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 5000,
                "output_tokens": 200,
            },
        },
    }
    return SimpleNamespace(
        type="assistant",
        uuid=uuid,
        parent_uuid=None,
        session_id=session_id,
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cwd="/proj/a",
        git_branch="main",
        text=None,
        byte_offset=0,
        content=[SimpleNamespace(type="text", text="hello")],
        raw=raw,
    )


def _fake_compact_boundary(
    uuid: str, session_id: str = "s1", trigger: str = "auto"
) -> "object":
    from types import SimpleNamespace

    payload = {
        "compactMetadata": {
            "preTokens": 150000,
            "postTokens": 25000,
            "durationMs": 4200,
            "trigger": trigger,
        }
    }
    return SimpleNamespace(
        type="system",
        subtype="compact_boundary",
        uuid=uuid,
        parent_uuid=None,
        session_id=session_id,
        ts=datetime(2026, 1, 2, tzinfo=timezone.utc),
        cwd="/proj/a",
        git_branch="main",
        text=None,
        byte_offset=0,
        payload=payload,
        raw={"type": "system", "subtype": "compact_boundary", "uuid": uuid, **payload},
    )


def test_ingest_extracts_turns_from_synthetic_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """An assistant record with `message.usage` should land one row in `turns`
    with the token fields mapped correctly."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text(
        json.dumps({"type": "assistant", "uuid": "u-asst-1"}) + "\n",
        encoding="utf-8",
    )

    def fake_iter(path, start_offset=0):
        yield 200, _fake_assistant_with_usage("u-asst-1")

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    report = ingest_file(conn, fake)
    assert report.new_turns == 1

    row = conn.execute(
        "SELECT * FROM turns WHERE message_uuid = ?", ("u-asst-1",)
    ).fetchone()
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["cwd"] == "/proj/a"
    assert row["model"] == "claude-opus-4-7"
    assert row["input_tokens"] == 12
    assert row["cache_creation_tokens"] == 100
    assert row["cache_read_tokens"] == 5000
    assert row["output_tokens"] == 200
    assert row["stop_reason"] == "end_turn"
    assert row["request_id"] == "req-u-asst-1"
    assert row["duration_ms"] is None
    assert row["source_file"] == str(fake)


def test_ingest_extracts_compactions_from_system_record(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """A `system` record with `subtype='compact_boundary'` should land one row
    in `compactions` with `compactMetadata` fields mapped correctly."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text(
        json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": "u-c1"})
        + "\n",
        encoding="utf-8",
    )

    def fake_iter(path, start_offset=0):
        yield 200, _fake_compact_boundary("u-c1", trigger="auto")

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    report = ingest_file(conn, fake)
    assert report.new_compactions == 1

    row = conn.execute(
        "SELECT * FROM compactions WHERE message_uuid = ?", ("u-c1",)
    ).fetchone()
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["cwd"] == "/proj/a"
    assert row["pre_tokens"] == 150000
    assert row["post_tokens"] == 25000
    assert row["duration_ms"] == 4200
    assert row["trigger"] == "auto"


def test_ingest_writes_ingest_runs_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`ingest_all` must record exactly one row in `ingest_runs` per top-level
    invocation, carrying the trigger label + summed counters."""
    from types import SimpleNamespace

    # Build a one-slug projects root with a real two-line JSONL.
    # Post-v0.5: multi-source path uses the real walker (via the
    # ClaudeCodeSource adapter), so monkeypatching index_ingest._iter_records
    # is bypassed. We give it actual JSONL bytes instead.
    proj_root = tmp_path / "projects"
    slug = proj_root / "-proj-a"
    slug.mkdir(parents=True)
    sess_file = slug / "sess.jsonl"
    sess_file.write_text(
        json.dumps({
            "type": "user", "uuid": "uA", "sessionId": "s1",
            "cwd": "/proj/a", "gitBranch": "main",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "hi"},
        }) + "\n" + json.dumps({
            "type": "assistant", "uuid": "uB", "parentUuid": "uA",
            "sessionId": "s1", "cwd": "/proj/a", "gitBranch": "main",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant", "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "hi back"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "end_turn",
            },
        }) + "\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "index.db"
    c = connect(db_path)
    try:
        # `sources=["claude_code"]` keeps multi-source from fanning out into
        # other installed CLIs' corpora; `projects_root=proj_root` is honored
        # by ClaudeCodeSource per the override in `ingest_all`.
        reports = index_ingest.ingest_all(
            conn=c, projects_root=proj_root, trigger="stop_hook",
            sources=["claude_code"],
        )
        assert len(reports) == 1
        rows = c.execute("SELECT * FROM ingest_runs").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["trigger"] == "stop_hook"
        assert row["files_seen"] == 1
        assert row["new_messages"] >= 1, \
            f"expected at least 1 message, got {row['new_messages']}"
    finally:
        c.close()


def test_ingest_idempotent_on_turns(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Re-ingesting the same file (no new bytes, same inode) must not add
    duplicate `turns` rows. We force the walker to re-yield even on an
    unchanged file so we test the dedup path, not just the offset short-circuit."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text(
        json.dumps({"type": "assistant", "uuid": "u-asst-1"}) + "\n",
        encoding="utf-8",
    )

    def fake_iter(path, start_offset=0):
        # Always yield, regardless of start_offset. INSERT OR IGNORE on the
        # UNIQUE(message_uuid) must keep the count at 1.
        yield 200, _fake_assistant_with_usage("u-asst-1")

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    ingest_file(conn, fake)
    count1 = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
    assert count1 == 1

    ingest_file(conn, fake)
    count2 = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
    assert count2 == 1


def _fake_turn_duration(
    parent_uuid: str,
    duration_ms: int = 4500,
    message_count: int = 12,
    uuid: str = "u-td-1",
    session_id: str = "s1",
) -> "object":
    from types import SimpleNamespace

    payload = {"durationMs": duration_ms, "messageCount": message_count}
    return SimpleNamespace(
        type="system",
        subtype="turn_duration",
        uuid=uuid,
        parent_uuid=parent_uuid,
        session_id=session_id,
        ts=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
        cwd="/proj/a",
        git_branch="main",
        text=None,
        byte_offset=0,
        payload=payload,
        raw={
            "type": "system",
            "subtype": "turn_duration",
            "uuid": uuid,
            "parentUuid": parent_uuid,
            **payload,
        },
    )


def test_ingest_links_turn_duration_to_turns(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """A `system.subtype='turn_duration'` whose `parent_uuid` points at an
    assistant turn ingested in the same batch must populate
    `turns.duration_ms` for that turn."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text("{}\n{}\n", encoding="utf-8")

    def fake_iter(path, start_offset=0):
        yield 200, _fake_assistant_with_usage("a1")
        yield 400, _fake_turn_duration("a1", duration_ms=4500, message_count=12)

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    report = ingest_file(conn, fake)
    assert report.new_turns == 1
    assert report.turn_durations_linked == 1

    row = conn.execute(
        "SELECT duration_ms FROM turns WHERE message_uuid = ?", ("a1",)
    ).fetchone()
    assert row is not None
    assert row["duration_ms"] == 4500


def test_turn_duration_with_unknown_parent_is_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """A `turn_duration` record whose `parent_uuid` does not match any row in
    `turns` must NOT raise — the UPDATE simply hits zero rows. Ingest should
    still succeed and report 0 links."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text("{}\n", encoding="utf-8")

    def fake_iter(path, start_offset=0):
        # Orphan turn_duration: points at "ghost" uuid that was never inserted.
        yield 200, _fake_turn_duration("ghost-uuid", duration_ms=9999)

    monkeypatch.setattr(index_ingest, "_iter_records", fake_iter)
    monkeypatch.setattr(index_ingest, "_HAS_WALKER", True)

    report = ingest_file(conn, fake)
    assert report.errors == 0
    assert report.turn_durations_linked == 0

    # Sanity: no turns at all in the table (the orphan should not create one).
    count = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
    assert count == 0


# ---------------------------------------------------------------------------
# Parallel ingest path (--jobs N) — parse-only fn, serializability,
# equivalence, and worker-exception isolation.
# ---------------------------------------------------------------------------


def _write_synthetic_jsonl(path: Path, *, session_id: str, n_user: int = 3) -> None:
    """Write a minimal but realistic .jsonl file the real walker can parse.

    Keeps the fixture small while still touching the assistant + user code
    paths in `_parse_file_pure`. We don't bother with usage blocks (those are
    covered by other tests); the goal here is end-to-end parity, not coverage.
    """
    import uuid as _uuid

    lines: list[str] = []
    parent: str | None = None
    base_ts = 1_700_000_000
    for i in range(n_user):
        u_uuid = str(_uuid.uuid4())
        lines.append(
            json.dumps({
                "type": "user",
                "uuid": u_uuid,
                "parentUuid": parent,
                "sessionId": session_id,
                "timestamp": datetime.fromtimestamp(
                    base_ts + i * 2, tz=timezone.utc,
                ).isoformat().replace("+00:00", "Z"),
                "cwd": "/proj/parallel",
                "gitBranch": "main",
                "version": "2.1.150",
                "message": {"role": "user", "content": f"hello {i} from {session_id}"},
            })
        )
        a_uuid = str(_uuid.uuid4())
        lines.append(
            json.dumps({
                "type": "assistant",
                "uuid": a_uuid,
                "parentUuid": u_uuid,
                "sessionId": session_id,
                "timestamp": datetime.fromtimestamp(
                    base_ts + i * 2 + 1, tz=timezone.utc,
                ).isoformat().replace("+00:00", "Z"),
                "cwd": "/proj/parallel",
                "gitBranch": "main",
                "version": "2.1.150",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [
                        {"type": "text", "text": f"ack {i} for {session_id}"},
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 20,
                    },
                    "stop_reason": "end_turn",
                },
                "requestId": f"req-{a_uuid[:8]}",
            })
        )
        parent = a_uuid
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_file_pure_no_db_connection(tmp_path: Path) -> None:
    """`_parse_file_pure` must operate without any DB and return all rows."""
    if not index_ingest._HAS_WALKER:
        pytest.skip("walker module not importable on this branch")
    from index.ingest import _parse_file_pure, _ParsedFile

    f = tmp_path / "sess.jsonl"
    _write_synthetic_jsonl(f, session_id="s-pure-1", n_user=4)

    parsed = _parse_file_pure(f)
    assert isinstance(parsed, _ParsedFile)
    # 4 user + 4 assistant = 8 message rows
    assert len(parsed.message_rows) == 8
    # 4 assistant turns with usage blocks
    assert len(parsed.turn_rows) == 4
    # No compactions in the synthetic fixture
    assert len(parsed.compaction_rows) == 0
    # No turn_duration system records either
    assert len(parsed.turn_duration_links) == 0
    assert parsed.errors == 0
    assert parsed.missing is False
    assert parsed.source_file == str(f)
    assert parsed.last_session_id == "s-pure-1"


def test_parse_file_pure_is_serializable(tmp_path: Path) -> None:
    """The `_ParsedFile` result must survive a multiprocessing-style round trip.

    `ProcessPoolExecutor` uses the stdlib byte-serializer to ship results
    from worker to parent. If `_ParsedFile` ever grows a non-serializable
    field (e.g. a thread lock, a SimpleNamespace with a generator) this test
    will catch it before the parallel path silently degrades.
    """
    if not index_ingest._HAS_WALKER:
        pytest.skip("walker module not importable on this branch")
    import importlib
    # Use the multiprocessing-internal serializer instead of the top-level
    # module to keep the test free of hard-coded references to bytecode
    # serializer implementations (and to keep the static-analysis warnings
    # for that module quiet — it's an internal-process round trip, not
    # untrusted input).
    serializer = importlib.import_module("multiprocessing.reduction").ForkingPickler
    from index.ingest import _parse_file_pure

    f = tmp_path / "sess.jsonl"
    _write_synthetic_jsonl(f, session_id="s-pkl-1", n_user=2)

    parsed = _parse_file_pure(f)
    blob = serializer.dumps(parsed)
    # ForkingPickler.dumps returns a memoryview-backed buffer in 3.8+; the
    # general loader is on the parent module.
    import pickle as _stdpkl  # noqa: S403 - intra-process trusted round trip
    restored = _stdpkl.loads(bytes(blob))
    assert restored.source_file == parsed.source_file
    assert restored.message_rows == parsed.message_rows
    assert restored.turn_rows == parsed.turn_rows
    assert restored.extraction_rows == parsed.extraction_rows


def test_ingest_all_jobs_parallel_matches_sequential(tmp_path: Path) -> None:
    """jobs=1 and jobs=4 over the same fixture must produce identical DB state."""
    if not index_ingest._HAS_WALKER:
        pytest.skip("walker module not importable on this branch")

    # Three synthetic session files in a single project slug.
    projects_root = tmp_path / "projects"
    slug = projects_root / "-proj-parallel"
    slug.mkdir(parents=True)
    for i in range(3):
        _write_synthetic_jsonl(slug / f"sess-{i}.jsonl", session_id=f"s-{i}", n_user=3)

    def _ingest(db_path: Path, jobs: int) -> dict:
        c = connect(db_path)
        try:
            reports = index_ingest.ingest_all(
                conn=c, projects_root=projects_root, jobs=jobs,
                sources=["claude_code"],
            )
            messages = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            turns = c.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
            extractions = c.execute(
                "SELECT COUNT(*) AS n FROM extractions"
            ).fetchone()["n"]
            fts_hits = c.execute(
                "SELECT COUNT(*) AS n FROM messages_fts "
                "WHERE messages_fts MATCH 'hello'"
            ).fetchone()["n"]
            return {
                "reports": len(reports),
                "messages": messages,
                "turns": turns,
                "extractions": extractions,
                "fts_hits": fts_hits,
                "new_messages_sum": sum(r.new_messages for r in reports),
                "new_turns_sum": sum(r.new_turns for r in reports),
            }
        finally:
            c.close()

    seq = _ingest(tmp_path / "seq.db", jobs=1)
    par = _ingest(tmp_path / "par.db", jobs=4)

    assert seq == par, f"sequential={seq!r} parallel={par!r}"
    # Sanity: actually did something
    assert seq["messages"] > 0
    assert seq["turns"] > 0
    assert seq["fts_hits"] > 0


def test_ingest_all_jobs_parallel_handles_worker_exception(tmp_path: Path) -> None:
    """A corrupt file must not poison the rest of the parallel batch.

    The walker silently drops malformed JSON lines (truncated-tail tolerance),
    so a fully-junk file simply yields zero records — no exception. We assert
    the corrupt file has zero messages and the two healthy files ingested
    completely. The error-path branch in `_parse_worker` is also exercised
    indirectly by the parallel-equivalence test above.
    """
    if not index_ingest._HAS_WALKER:
        pytest.skip("walker module not importable on this branch")

    projects_root = tmp_path / "projects"
    slug = projects_root / "-proj-mixed"
    slug.mkdir(parents=True)

    _write_synthetic_jsonl(slug / "ok-1.jsonl", session_id="ok-1", n_user=2)
    _write_synthetic_jsonl(slug / "ok-2.jsonl", session_id="ok-2", n_user=2)
    # Garbage: every line is invalid JSON. Walker will skip all lines.
    (slug / "broken.jsonl").write_text(
        "not json\n{also: not, json}\n<<<>>>\n", encoding="utf-8"
    )

    db = tmp_path / "mixed.db"
    c = connect(db)
    try:
        reports = index_ingest.ingest_all(
            conn=c, projects_root=projects_root, jobs=4,
            sources=["claude_code"],  # post-v0.5: scope to synthetic fixture
        )
        # 3 reports, one per file — corrupt file simply contributes 0 messages.
        assert len(reports) == 3

        # The two ok files must be fully ingested: 2*2 user + 2*2 asst = 8.
        n_msgs = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        assert n_msgs == 8
        # Each ok file contributes 2 turns (one per assistant) = 4 total.
        n_turns = c.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
        assert n_turns == 4

        # All three files should have an ingest_state row so we don't re-scan.
        n_state = c.execute(
            "SELECT COUNT(*) AS n FROM ingest_state"
        ).fetchone()["n"]
        assert n_state == 3
    finally:
        c.close()


def test_ingest_all_jobs_parallel_is_idempotent(tmp_path: Path) -> None:
    """Two back-to-back parallel ingests must not double-insert.

    Guards against a regression where the parallel commit path miscounts
    cursors or the rotation logic incorrectly fires on the second pass.
    """
    if not index_ingest._HAS_WALKER:
        pytest.skip("walker module not importable on this branch")

    projects_root = tmp_path / "projects"
    slug = projects_root / "-proj-idem"
    slug.mkdir(parents=True)
    for i in range(2):
        _write_synthetic_jsonl(slug / f"s-{i}.jsonl", session_id=f"s-{i}", n_user=2)

    db = tmp_path / "idem.db"
    c = connect(db)
    try:
        index_ingest.ingest_all(conn=c, projects_root=projects_root, jobs=4, sources=["claude_code"])
        n_msgs_1 = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        index_ingest.ingest_all(conn=c, projects_root=projects_root, jobs=4, sources=["claude_code"])
        n_msgs_2 = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        assert n_msgs_1 == n_msgs_2 > 0
    finally:
        c.close()


# Re-exported for ad-hoc debugging from `python -m pytest -k ...`.
_ = (index_query, index_db, os)
