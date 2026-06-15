"""Tests for XW8 — multi-source ingest + cross-source dedup.

Hermetic: no real ``~/.claude/projects`` access. We register a synthetic
``test_source`` adapter against :data:`lib.sources.base.SOURCES`, drive
:func:`index.ingest.ingest_all` against it, and inspect the resulting
``messages`` / ``extractions`` rows for the new ``source`` column and the
dedup behaviour described in :mod:`index.multi_source`.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from index import ingest as index_ingest  # noqa: E402
from index.db import apply_schema, connect  # noqa: E402
from index.ingest import ingest_all  # noqa: E402
from index.multi_source import (  # noqa: E402
    dedup_key,
    filter_dedup_rows,
    minute_bucket,
    should_suppress,
    source_priority,
    text_hash,
)
from lib.sources.base import SOURCES, SessionFile, SessionSource  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic record + adapter
# ---------------------------------------------------------------------------


@dataclass
class _SyntheticRec:
    """Minimal stand-in for ``lib.schema.Record``.

    Carries just enough attributes for :func:`index.ingest._row_for_message`
    to build a ``messages`` row: ``type``, ``uuid``, ``session_id``,
    ``cwd``, ``ts`` (datetime), plus the type-specific ``text`` /
    ``content`` payload.
    """

    type: str
    uuid: str
    session_id: str
    cwd: str
    ts: datetime
    text: str
    parent_uuid: str | None = None
    git_branch: str | None = None
    byte_offset: int = 0
    content_kind: str = "string"
    tool_results: list = field(default_factory=list)
    content: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _make_synth_source(
    name: str,
    sessions: list[tuple[str, str, list[_SyntheticRec]]],
    *,
    available: bool = True,
    raise_on_is_available: bool = False,
) -> type[SessionSource]:
    """Build a SessionSource subclass whose discovery returns ``sessions``.

    ``sessions`` is a list of ``(session_id, cwd, records)`` triples. The
    returned class self-registers nothing — tests append it to
    :data:`SOURCES` themselves and clean up afterwards.
    """

    class _SynthSource(SessionSource):
        # Class attribute names cribbed from the spec.
        # Each instance reuses the closure's sessions list directly.

        def is_available(self) -> bool:
            if raise_on_is_available:
                raise RuntimeError("synthetic is_available crash")
            return available

        def discover_sessions(self) -> Iterator[SessionFile]:
            for sid, cwd, recs in sessions:
                yield SessionFile(
                    source=self.name,
                    # Each session gets its own synthetic path so the
                    # ingest_state keying is unambiguous.
                    path=Path(f"/tmp/_synth/{name}/{sid}.jsonl"),
                    cwd=cwd,
                    session_id=sid,
                    started_at=None,
                    last_modified=0.0,
                    extra={"_records": recs},
                )

        def iter_records(
            self, session: SessionFile, start_offset: int = 0
        ) -> Iterator[tuple[int, Any]]:
            recs = session.extra.get("_records", [])
            yield from enumerate(recs)

    _SynthSource.name = name
    _SynthSource.__name__ = f"_SynthSource_{name}"
    return _SynthSource


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh on-disk DB so FTS5 works."""
    c = connect(tmp_path / "index.db")
    yield c
    c.close()


@pytest.fixture
def clean_sources():
    """Snapshot + restore SOURCES around a test that mutates the registry."""
    original = list(SOURCES)
    try:
        yield
    finally:
        SOURCES.clear()
        SOURCES.extend(original)


# ---------------------------------------------------------------------------
# Pure helpers in index.multi_source
# ---------------------------------------------------------------------------


def test_source_priority_known_and_unknown():
    assert source_priority("claude_code") == 0
    assert source_priority("codex") == 1
    assert source_priority("opencode") == 2
    assert source_priority("aider") == 7
    # Unknown names sort *after* every known source.
    assert source_priority("zzz") > source_priority("aider")
    assert source_priority(None) > source_priority("aider")


def test_should_suppress_prefers_higher_priority():
    # claude_code beats codex → codex candidate should be suppressed.
    assert should_suppress("codex", "claude_code") is True
    # Reverse → no suppression.
    assert should_suppress("claude_code", "codex") is False
    # Equal priority → never suppress (keeps both).
    assert should_suppress("codex", "codex") is False


def test_minute_bucket_and_text_hash():
    assert minute_bucket(0) == 0
    assert minute_bucket(59) == 0
    assert minute_bucket(60) == 1
    assert minute_bucket(None) is None
    assert minute_bucket("not-a-number") is None

    h1 = text_hash("hello world")
    h2 = text_hash("hello world")
    assert h1 == h2 and isinstance(h1, str) and len(h1) == 64
    # First 200 chars are what's hashed; trailing differences don't matter.
    a = "x" * 200 + "AAA"
    b = "x" * 200 + "BBB"
    assert text_hash(a) == text_hash(b)
    # Empty / whitespace → None.
    assert text_hash("") is None
    assert text_hash("   ") is None


def test_dedup_key_requires_all_components():
    assert dedup_key(None, 1000, "hi") is None
    assert dedup_key("/cwd", None, "hi") is None
    assert dedup_key("/cwd", 1000, "") is None
    k = dedup_key("/cwd", 1000, "hi")
    assert k == ("/cwd", 1000 // 60, text_hash("hi"))


def test_filter_dedup_rows_basic_suppression():
    # Row shape: (cwd, ts, text). cwd_idx=0, ts_idx=1, text_idx=2.
    rows_cc = [("/proj", 1000, "shared message")]
    rows_co = [("/proj", 1005, "shared message")]  # same minute bucket
    kept_cc, seen, supp_cc = filter_dedup_rows(
        rows_cc, source="claude_code",
        cwd_idx=0, ts_idx=1, text_idx=2,
    )
    assert kept_cc == rows_cc and supp_cc == 0

    kept_co, _, supp_co = filter_dedup_rows(
        rows_co, source="continue",
        cwd_idx=0, ts_idx=1, text_idx=2,
        seen=seen,
    )
    # continue is lower priority than claude_code → the codex/continue row
    # should be suppressed in favor of the already-seen claude_code one.
    assert kept_co == [] and supp_co == 1


def test_filter_dedup_rows_better_source_replaces_owner():
    # Initial low-priority source claims the key.
    rows_low = [("/proj", 1000, "shared")]
    kept_low, seen, _ = filter_dedup_rows(
        rows_low, source="continue",
        cwd_idx=0, ts_idx=1, text_idx=2,
    )
    assert kept_low == rows_low
    assert seen[dedup_key("/proj", 1000, "shared")] == "continue"

    # High-priority source comes along: row is kept AND seen is updated.
    rows_high = [("/proj", 1000, "shared")]
    kept_high, seen2, supp = filter_dedup_rows(
        rows_high, source="claude_code",
        cwd_idx=0, ts_idx=1, text_idx=2,
        seen=seen,
    )
    assert kept_high == rows_high and supp == 0
    assert seen2[dedup_key("/proj", 1000, "shared")] == "claude_code"


def test_filter_dedup_rows_unbuildable_key_keeps_row():
    # Missing cwd → no dedup possible → row is kept.
    rows = [(None, 1000, "hi"), ("/proj", None, "hi"), ("/proj", 1000, "")]
    kept, _, supp = filter_dedup_rows(
        rows, source="continue",
        cwd_idx=0, ts_idx=1, text_idx=2,
    )
    assert kept == rows and supp == 0


# ---------------------------------------------------------------------------
# Schema migration (v3 → v4)
# ---------------------------------------------------------------------------


def test_schema_v3_to_v4_migration(tmp_path: Path) -> None:
    """A pre-v4 DB without source columns should pick them up on apply_schema."""
    db_path = tmp_path / "v3.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT,
            git_branch TEXT, role TEXT NOT NULL, kind TEXT, ts INTEGER,
            parent_uuid TEXT, message_uuid TEXT UNIQUE,
            byte_offset INTEGER NOT NULL, source_file TEXT NOT NULL,
            text TEXT, raw_json BLOB);
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            session_id TEXT NOT NULL, cwd TEXT, ts INTEGER,
            source_uuid TEXT, score REAL DEFAULT 0.5,
            scope TEXT DEFAULT 'project', context_json TEXT,
            UNIQUE(kind, source_uuid));
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3');
        INSERT INTO messages(session_id, role, byte_offset, source_file, text)
            VALUES ('s1', 'user', 0, '/tmp/x', 'legacy row');
        INSERT INTO extractions(kind, content, session_id)
            VALUES ('decision', 'old', 's1');
        """
    )
    raw.commit()
    raw.close()

    c = connect(db_path)
    try:
        # Columns exist after migration.
        msg_cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        assert "source" in msg_cols
        assert "dedup_superseded_by_source" in msg_cols
        ext_cols = {r["name"] for r in c.execute("PRAGMA table_info(extractions)")}
        assert "source" in ext_cols
        assert "dedup_superseded_by_source" in ext_cols
        # Legacy rows default to 'claude_code'.
        row = c.execute("SELECT source FROM messages WHERE text = 'legacy row'").fetchone()
        assert row["source"] == "claude_code"
        # Version is now v4.
        ver = c.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert ver["value"] == "5"

        # Re-applying must be a no-op.
        apply_schema(c)
        ver2 = c.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert ver2["value"] == "5"
        # Row count unchanged.
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        c.close()


# ---------------------------------------------------------------------------
# ingest_all multi-source path
# ---------------------------------------------------------------------------


def _mk_rec(uuid: str, session_id: str, cwd: str, ts_epoch: int, text: str) -> _SyntheticRec:
    return _SyntheticRec(
        type="user",
        uuid=uuid,
        session_id=session_id,
        cwd=cwd,
        ts=datetime.fromtimestamp(ts_epoch, tz=timezone.utc),
        text=text,
    )


def test_ingest_all_explicit_test_source_tags_rows(
    conn: sqlite3.Connection, clean_sources
) -> None:
    """sources=['test_source'] only ingests the named adapter and tags rows."""
    rec = _mk_rec("u1", "synth-session-1", "/proj/a", 1000, "from synthetic source")
    Synth = _make_synth_source(
        "test_source", [("synth-session-1", "/proj/a", [rec])]
    )
    SOURCES.append(Synth)

    reports = ingest_all(conn=conn, sources=["test_source"])
    assert reports  # at least one
    assert sum(r.new_messages for r in reports) >= 1

    rows = conn.execute(
        "SELECT source, text FROM messages WHERE session_id = ?",
        ("synth-session-1",),
    ).fetchall()
    assert [r["source"] for r in rows] == ["test_source"]
    assert any("synthetic" in (r["text"] or "") for r in rows)


def test_ingest_all_multiple_sources_writes_both_tags(
    conn: sqlite3.Connection, clean_sources
) -> None:
    """sources=['claude_code', 'test_source'] writes rows tagged with each."""
    rec_cc = _mk_rec("u-cc-1", "cc-1", "/proj/p", 2000, "claude row")
    rec_ts = _mk_rec("u-ts-1", "ts-1", "/proj/p", 2500, "synth row")

    SynthCC = _make_synth_source(
        "claude_code", [("cc-1", "/proj/p", [rec_cc])]
    )
    SynthTS = _make_synth_source(
        "test_source", [("ts-1", "/proj/p", [rec_ts])]
    )
    # Replace registry so the real ClaudeCodeSource doesn't fight ours.
    SOURCES.clear()
    SOURCES.extend([SynthCC, SynthTS])

    ingest_all(conn=conn, sources=["claude_code", "test_source"])

    sources_seen = {
        r["source"]
        for r in conn.execute("SELECT DISTINCT source FROM messages")
    }
    assert "claude_code" in sources_seen
    assert "test_source" in sources_seen


def test_ingest_all_cross_source_dedup_prefers_higher_priority(
    conn: sqlite3.Connection, clean_sources
) -> None:
    """Same content in claude_code and continue → only claude_code persists."""
    # Same cwd, same minute bucket (within 60s), same text prefix.
    rec_cc = _mk_rec("u-cc-x", "s-cc", "/proj/dup", 5000, "shared dup body")
    rec_ct = _mk_rec("u-ct-x", "s-ct", "/proj/dup", 5005, "shared dup body")

    SynthCC = _make_synth_source(
        "claude_code", [("s-cc", "/proj/dup", [rec_cc])]
    )
    SynthCT = _make_synth_source(
        "continue", [("s-ct", "/proj/dup", [rec_ct])]
    )
    SOURCES.clear()
    SOURCES.extend([SynthCC, SynthCT])  # priority order: CC first

    ingest_all(conn=conn, sources=["claude_code", "continue"])

    rows = conn.execute(
        "SELECT source, text FROM messages WHERE cwd = ?",
        ("/proj/dup",),
    ).fetchall()
    # Exactly one row should survive — the claude_code one.
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["source"] == "claude_code"


def test_ingest_all_no_dedup_when_content_differs(
    conn: sqlite3.Connection, clean_sources
) -> None:
    """Different content under the same cwd/minute → BOTH rows persist."""
    rec_cc = _mk_rec("u-cc-y", "s-cc-y", "/proj/diff", 6000, "claude says A")
    rec_ct = _mk_rec("u-ct-y", "s-ct-y", "/proj/diff", 6005, "continue says B")
    SynthCC = _make_synth_source("claude_code", [("s-cc-y", "/proj/diff", [rec_cc])])
    SynthCT = _make_synth_source("continue", [("s-ct-y", "/proj/diff", [rec_ct])])
    SOURCES.clear()
    SOURCES.extend([SynthCC, SynthCT])

    ingest_all(conn=conn, sources=["claude_code", "continue"])

    rows = conn.execute(
        "SELECT source FROM messages WHERE cwd = ? ORDER BY source",
        ("/proj/diff",),
    ).fetchall()
    assert {r["source"] for r in rows} == {"claude_code", "continue"}


def test_ingest_all_skips_source_whose_is_available_raises(
    conn: sqlite3.Connection, clean_sources, caplog
) -> None:
    """A buggy adapter must not break the others when sources=None."""
    rec = _mk_rec("u-ok", "s-ok", "/proj/ok", 7000, "good source row")
    Good = _make_synth_source("test_source", [("s-ok", "/proj/ok", [rec])])
    Bad = _make_synth_source(
        "bad_source", [("s-bad", "/proj/bad", [])],
        raise_on_is_available=True,
    )
    SOURCES.clear()
    SOURCES.extend([Bad, Good])

    # No explicit sources → must auto-filter to available ones.
    with caplog.at_level("WARNING"):
        ingest_all(conn=conn, sources=None)

    rows = conn.execute("SELECT DISTINCT source FROM messages").fetchall()
    assert {r["source"] for r in rows} == {"test_source"}
    # And the buggy source got a warning rather than crashing the call.
    assert any("bad_source" in rec.message for rec in caplog.records)


def test_unknown_source_name_is_logged_and_skipped(
    conn: sqlite3.Connection, clean_sources, caplog
) -> None:
    """sources=['no-such-source'] logs but doesn't raise."""
    SOURCES.clear()  # nothing registered
    with caplog.at_level("WARNING"):
        reports = ingest_all(conn=conn, sources=["no-such-source"])
    assert reports == []
    assert any("no-such-source" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Hook compatibility — Stop / PostCompact call ingest_all(connect())
# ---------------------------------------------------------------------------


def test_hook_style_invocation_still_works(
    conn: sqlite3.Connection, clean_sources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Stop / PostCompact hooks call ``ingest_all(connect())`` with
    no kwargs. After XW8 that path must still work: default sources=None
    picks up every available adapter, including any new ones."""
    rec = _mk_rec("u-hook", "s-hook", "/proj/hook", 8000, "hook-triggered row")
    Synth = _make_synth_source("test_source", [("s-hook", "/proj/hook", [rec])])
    SOURCES.clear()
    SOURCES.append(Synth)

    # Mirror what stop-index.sh / post-compact-index.sh actually invoke:
    #     from index.db import connect
    #     from index.ingest import ingest_all
    #     ingest_all(connect())
    # We use the test's conn instead of opening a fresh one but the
    # signature shape is what matters.
    ingest_all(conn)

    rows = conn.execute(
        "SELECT source, session_id FROM messages WHERE cwd = ?",
        ("/proj/hook",),
    ).fetchall()
    assert any(r["source"] == "test_source" for r in rows)


# ---------------------------------------------------------------------------
# Source file key for SQLite-backed adapters
# ---------------------------------------------------------------------------


def test_source_file_key_disambiguates_sqlite_sessions():
    sf_a = SessionFile(
        source="opencode", path=Path("/data/opencode.db"),
        cwd="/proj/a", session_id="ses-A",
        started_at=None, last_modified=0.0,
        extra={"storage": "sqlite"},
    )
    sf_b = SessionFile(
        source="opencode", path=Path("/data/opencode.db"),
        cwd="/proj/b", session_id="ses-B",
        started_at=None, last_modified=0.0,
        extra={"storage": "sqlite"},
    )
    sf_file = SessionFile(
        source="claude_code", path=Path("/x/y.jsonl"),
        cwd="/proj/c", session_id="ses-file",
        started_at=None, last_modified=0.0,
    )
    a = index_ingest._source_file_key_for_session(sf_a)
    b = index_ingest._source_file_key_for_session(sf_b)
    f = index_ingest._source_file_key_for_session(sf_file)
    # Same DB, different sessions → distinct keys (key includes session_id).
    assert a != b
    assert a == "/data/opencode.db#ses-A"
    assert b == "/data/opencode.db#ses-B"
    # File-per-session adapter just uses the path.
    assert f == "/x/y.jsonl"
