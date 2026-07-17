"""Tests for `index.metrics` — aggregation helpers behind `total-recall metrics`.

These tests build small synthetic DBs on disk (FTS5 + WAL behaves better
on disk than in `:memory:`) and assert both the v2-schema path and the
graceful-degradation path when the v2 tables (turns, compactions,
ingest_runs) are missing.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from index import metrics as M  # noqa: E402
from index.db import connect  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _insert_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    cwd: str,
    ts: int,
    text: str,
    uuid: str,
    role: str = "assistant",
    kind: str | None = "assistant",
) -> None:
    conn.execute(
        """
        INSERT INTO messages(
            session_id, cwd, git_branch, role, kind, ts,
            parent_uuid, message_uuid, byte_offset, source_file, text, raw_json
        ) VALUES (?, ?, 'main', ?, ?, ?, NULL, ?, 0, '/tmp/x.jsonl', ?, NULL)
        """,
        (session_id, cwd, role, kind, ts, uuid, text),
    )


def _insert_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    cwd: str,
    ts: int,
    model: str = "claude-opus-4-7",
    input_tokens: int = 1000,
    cache_read_tokens: int = 500,
    cache_creation_tokens: int = 0,
    output_tokens: int = 200,
    duration_ms: int = 5000,
    uuid: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO turns(
            session_id, cwd, ts, model,
            input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens,
            duration_ms, stop_reason, request_id, message_uuid, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'end_turn', 'req', ?, '/tmp/x.jsonl')
        """,
        (
            session_id,
            cwd,
            ts,
            model,
            input_tokens,
            cache_creation_tokens,
            cache_read_tokens,
            output_tokens,
            duration_ms,
            uuid or f"turn-{session_id}-{ts}",
        ),
    )


def _insert_compaction(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    cwd: str,
    ts: int,
    pre_tokens: int = 100_000,
    post_tokens: int = 20_000,
    uuid: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO compactions(
            session_id, cwd, ts, pre_tokens, post_tokens, duration_ms,
            trigger, message_uuid, source_file
        ) VALUES (?, ?, ?, ?, ?, 1500, 'auto', ?, '/tmp/x.jsonl')
        """,
        (session_id, cwd, ts, pre_tokens, post_tokens, uuid or f"compact-{session_id}-{ts}"),
    )


def _insert_ingest_run(
    conn: sqlite3.Connection,
    *,
    ts: int,
    elapsed_ms: int,
    errors: int = 0,
    new_messages: int = 10,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_runs(
            ts, files_seen, files_new, new_messages, new_extractions,
            new_turns, new_compactions, elapsed_ms, errors, trigger
        ) VALUES (?, 5, 2, ?, 3, 4, 0, ?, ?, 'manual')
        """,
        (ts, new_messages, elapsed_ms, errors),
    )


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
) -> None:
    conn.execute(
        """
        INSERT INTO extractions(
            kind, content, session_id, cwd, ts, source_uuid,
            score, scope, context_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'project', '{}')
        """,
        (kind, content, session_id, cwd, ts, source_uuid, score),
    )


@pytest.fixture()
def v2_db(tmp_path: Path) -> Path:
    """On-disk DB with the full v2 schema and a small synthetic corpus."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    base = _now() - 3600  # an hour ago, well inside any "7d" window
    cwd_a = "/proj/alpha"
    cwd_b = "/proj/beta"

    # Three sessions, two cwds. ~100 turns total spread across them.
    sessions_layout = [
        ("sess-1", cwd_a, 60),  # biggest by tokens
        ("sess-2", cwd_a, 30),
        ("sess-3", cwd_b, 10),
    ]
    uuid_counter = 0
    for sid, cwd, n_turns in sessions_layout:
        # Anchor each session with an ai-title message + bracketing messages
        # so the wall-clock fields are non-trivial.
        _insert_message(
            conn,
            session_id=sid,
            cwd=cwd,
            ts=base,
            text=f"title-for-{sid}",
            uuid=f"{sid}-title",
            role="assistant",
            kind="ai-title",
        )
        _insert_message(
            conn,
            session_id=sid,
            cwd=cwd,
            ts=base + 1,
            text="first",
            uuid=f"{sid}-first",
            role="user",
            kind="user",
        )
        _insert_message(
            conn,
            session_id=sid,
            cwd=cwd,
            ts=base + 60 * n_turns,
            text="last",
            uuid=f"{sid}-last",
            role="user",
            kind="user",
        )
        for i in range(n_turns):
            uuid_counter += 1
            _insert_turn(
                conn,
                session_id=sid,
                cwd=cwd,
                ts=base + i * 60,
                model="claude-opus-4-7" if i % 2 == 0 else "claude-sonnet-4-6",
                input_tokens=1000,
                cache_read_tokens=400,
                cache_creation_tokens=100,
                output_tokens=200,
                duration_ms=5_000,
                uuid=f"turn-{uuid_counter}",
            )

    # A couple of compactions on sess-1.
    _insert_compaction(conn, session_id="sess-1", cwd=cwd_a, ts=base + 1000)
    _insert_compaction(conn, session_id="sess-1", cwd=cwd_a, ts=base + 2000)

    # Extractions: 3 corrections under the same content on sess-1, plus
    # other variety so `topics` has something to bucket.
    for i in range(3):
        _insert_extraction(
            conn,
            kind="correction",
            content="always use provider-y not provider-x",
            session_id="sess-1",
            cwd=cwd_a,
            ts=base + 100 + i,
            source_uuid=f"corr-{i}",
        )
    _insert_extraction(
        conn,
        kind="decision",
        content="deploy with terraform",
        session_id="sess-2",
        cwd=cwd_a,
        ts=base + 200,
        source_uuid="dec-1",
    )
    _insert_extraction(
        conn,
        kind="correction",
        content="rename the function please",
        session_id="sess-2",
        cwd=cwd_a,
        ts=base + 300,
        source_uuid="corr-x",
    )

    # Two ingest runs, one with an error, one without.
    _insert_ingest_run(conn, ts=base + 10, elapsed_ms=120, errors=0)
    _insert_ingest_run(conn, ts=base + 20, elapsed_ms=240, errors=1)

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def v1_db(tmp_path: Path) -> Path:
    """On-disk DB with only the v1 tables — no turns/compactions/ingest_runs."""
    db_path = tmp_path / "index_v1.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Only the bare minimum: messages + extractions.
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            cwd TEXT,
            git_branch TEXT,
            role TEXT NOT NULL,
            kind TEXT,
            ts INTEGER,
            parent_uuid TEXT,
            message_uuid TEXT UNIQUE,
            byte_offset INTEGER NOT NULL DEFAULT 0,
            source_file TEXT NOT NULL DEFAULT '',
            text TEXT,
            raw_json BLOB
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT,
            ts INTEGER,
            source_uuid TEXT,
            score REAL DEFAULT 0.5,
            scope TEXT DEFAULT 'project',
            context_json TEXT,
            UNIQUE(kind, source_uuid)
        );
        """
    )
    base = _now() - 3600
    _insert_message(
        conn,
        session_id="s1",
        cwd="/proj/x",
        ts=base,
        text="hello",
        uuid="s1-1",
    )
    _insert_extraction(
        conn,
        kind="correction",
        content="use provider-y",
        session_id="s1",
        cwd="/proj/x",
        ts=base,
        source_uuid="c1",
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


def test_parse_since_days() -> None:
    now = _now()
    val = M._parse_since("7d")
    # Allow ~5s clock drift.
    assert abs((now - val) - 7 * 86_400) < 5


def test_parse_since_hours() -> None:
    now = _now()
    val = M._parse_since("24h")
    assert abs((now - val) - 24 * 3600) < 5


def test_parse_since_minutes() -> None:
    now = _now()
    val = M._parse_since("30m")
    assert abs((now - val) - 30 * 60) < 5


def test_parse_since_iso_date() -> None:
    val = M._parse_since("2026-05-20")
    expected = int(datetime(2026, 5, 20, tzinfo=timezone.utc).timestamp())
    assert val == expected


def test_parse_since_empty_and_none() -> None:
    assert M._parse_since("") == 0
    assert M._parse_since(None) == 0


def test_parse_since_unknown_format_is_all_time() -> None:
    assert M._parse_since("garbage") == 0


# ---------------------------------------------------------------------------
# summary()
# ---------------------------------------------------------------------------


def test_summary_basic_v2(v2_db: Path) -> None:
    s = M.summary(v2_db, since="7d")
    # Three sessions across two projects.
    assert s["sessions"] == 3
    assert s["projects"] == 2
    assert s["compactions"] == 2
    # Total turns = 60+30+10 = 100; each turn = 1000 in, 400 cache_read,
    # 100 cache_create, 200 out.
    assert s["input_tokens"] == 100 * 1000
    assert s["cache_read_tokens"] == 100 * 400
    assert s["output_tokens"] == 100 * 200
    # cache_read_pct = 40000 / (100000 + 40000 + 10000) = 26.67%
    assert 25.0 < s["cache_read_pct"] < 30.0
    assert s["estimated_cost"] > 0.0
    # Each turn 5000ms, 100 turns → 500_000ms = 0.139h
    assert s["active_hours"] == pytest.approx(500_000 / 3_600_000.0, abs=0.01)
    assert s["top_corrections"]
    assert s["top_corrections"][0]["content"] == "always use provider-y not provider-x"
    assert s["top_corrections"][0]["count"] == 3
    assert s["busiest_project"]["cwd"] == "/proj/alpha"
    assert s["longest_session"]["session_id"] == "sess-1"
    assert s["longest_session"]["tokens"] > 0


def test_summary_respects_project_filter(v2_db: Path) -> None:
    s_alpha = M.summary(v2_db, since="7d", project="/proj/alpha")
    s_beta = M.summary(v2_db, since="7d", project="/proj/beta")
    assert s_alpha["sessions"] == 2
    assert s_beta["sessions"] == 1
    assert s_alpha["projects"] == 1
    assert s_alpha["input_tokens"] > s_beta["input_tokens"]


def test_summary_graceful_on_v1_schema(v1_db: Path) -> None:
    s = M.summary(v1_db, since="30d")
    # Sessions / projects still come from messages.
    assert s["sessions"] == 1
    assert s["projects"] == 1
    # All turn-derived fields are zero.
    assert s["input_tokens"] == 0
    assert s["cache_read_tokens"] == 0
    assert s["output_tokens"] == 0
    assert s["cache_read_pct"] == 0.0
    assert s["estimated_cost"] == 0.0
    assert s["compactions"] == 0
    assert s["active_hours"] == 0.0
    # But corrections (from extractions) still work.
    assert s["top_corrections"] and s["top_corrections"][0]["count"] == 1


# ---------------------------------------------------------------------------
# cost()
# ---------------------------------------------------------------------------


def test_cost_default_rates(v2_db: Path) -> None:
    c = M.cost(v2_db, since="7d")
    assert c["total_cost"] > 0.0
    models = {row["model"] for row in c["by_model"]}
    assert "claude-opus-4-7" in models
    assert "claude-sonnet-4-6" in models


def test_cost_override_rates_changes_total(v2_db: Path) -> None:
    baseline = M.cost(v2_db, since="7d")
    cheap = M.cost(
        v2_db,
        since="7d",
        rates={
            "claude-opus-4-7": (0.01, 0.01),
            "claude-sonnet-4-6": (0.01, 0.01),
            "default": (0.01, 0.01),
        },
    )
    assert cheap["total_cost"] < baseline["total_cost"]


def test_cost_graceful_on_v1_schema(v1_db: Path) -> None:
    c = M.cost(v1_db, since="30d")
    assert c == {"by_model": [], "total_cost": 0.0}


# ---------------------------------------------------------------------------
# sessions()
# ---------------------------------------------------------------------------


def test_sessions_rank_by_tokens(v2_db: Path) -> None:
    out = M.sessions(v2_db, top=3, by="tokens", since="7d")
    assert [r["session_id"] for r in out] == ["sess-1", "sess-2", "sess-3"]
    assert out[0]["tokens"] > out[1]["tokens"] > out[2]["tokens"]


def test_sessions_rank_by_duration(v2_db: Path) -> None:
    out = M.sessions(v2_db, top=3, by="duration", since="7d")
    # sess-1 has 60 turns at 60s spacing → 60min wall; sess-2 has 30 (30m);
    # sess-3 has 10 (10m).
    assert [r["session_id"] for r in out] == ["sess-1", "sess-2", "sess-3"]


def test_sessions_rank_by_corrections(v2_db: Path) -> None:
    out = M.sessions(v2_db, top=3, by="corrections", since="7d")
    # sess-1 has 3 corrections, sess-2 has 1, sess-3 has 0.
    assert out[0]["session_id"] == "sess-1"
    assert out[0]["corrections"] == 3
    assert out[1]["corrections"] == 1


def test_sessions_top_limit(v2_db: Path) -> None:
    out = M.sessions(v2_db, top=1, by="tokens", since="7d")
    assert len(out) == 1


def test_sessions_graceful_on_v1_schema(v1_db: Path) -> None:
    out = M.sessions(v1_db, top=5, by="tokens", since="30d")
    assert len(out) == 1
    assert out[0]["tokens"] == 0
    assert out[0]["session_id"] == "s1"


# ---------------------------------------------------------------------------
# topics()
# ---------------------------------------------------------------------------


def test_topics_non_empty(v2_db: Path) -> None:
    out = M.topics(v2_db, since="7d", limit=10)
    assert out
    top = out[0]
    assert top["topic"]
    assert top["count"] >= 1
    assert "correction" in top["kinds"] or "decision" in top["kinds"]
    # The 3 duplicate corrections should bucket together at the top.
    assert out[0]["count"] >= 3


def test_topics_respects_project_filter(v2_db: Path) -> None:
    out_alpha = M.topics(v2_db, since="7d", project="/proj/alpha")
    out_beta = M.topics(v2_db, since="7d", project="/proj/beta")
    assert out_alpha
    assert out_beta == []


def test_topics_graceful_on_v1_schema(v1_db: Path) -> None:
    out = M.topics(v1_db, since="30d")
    # v1 still has extractions, so we should get at least one bucket.
    assert out
    assert out[0]["count"] >= 1


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def test_health_v2(v2_db: Path) -> None:
    h = M.health(v2_db)
    assert h["total_messages"] >= 9  # 3 messages per session, 3 sessions
    assert h["total_extractions"] >= 5
    assert h["total_turns"] == 100
    assert h["total_compactions"] == 2
    assert h["ingest_runs_7d"] == 2
    assert h["error_count_7d"] == 1
    assert h["p50_ingest_ms"] > 0
    assert h["p95_ingest_ms"] >= h["p50_ingest_ms"]
    assert h["last_ingest_ts"] is not None
    assert h["last_ingest_age_seconds"] is not None
    assert h["db_size_bytes"] > 0


# ---------------------------------------------------------------------------
# _normalize_correction + grouped top_corrections
# ---------------------------------------------------------------------------


def test_normalize_correction_strips_trigger_words() -> None:
    # Two paraphrasings of the same standing rule should collapse to the
    # same normalized key.
    a = M._normalize_correction("no please use Edit not Write")
    b = M._normalize_correction("stop using Write, use Edit")
    # Sanity: keys are non-empty and don't include the stripped triggers.
    assert a
    assert b
    assert "no" not in a.split()
    assert "stop" not in b.split()
    # Both should be the same bucket. The exact characters depend on
    # heuristic ordering, so assert *equality* (not literal string).
    assert a == b
    # And the key should roughly look like "use edit ... write".
    assert "use" in a and "edit" in a and "write" in a


def test_normalize_correction_empty_after_strip_falls_back() -> None:
    # Input is just a trigger word — after stripping there's nothing left.
    # We must fall back to the lowercased original, not crash or return "".
    out = M._normalize_correction("no")
    assert out == "no"
    out2 = M._normalize_correction("")
    assert out2 == ""


def test_summary_top_corrections_groups_paraphrased(tmp_path: Path) -> None:
    db_path = tmp_path / "grouped.db"
    conn = connect(db_path)
    base = _now() - 600
    cwd = "/proj/grouped"

    # Anchor message so the session shows up.
    _insert_message(
        conn,
        session_id="s1",
        cwd=cwd,
        ts=base,
        text="anchor",
        uuid="s1-anchor",
        role="user",
        kind="user",
    )

    # 5 paraphrasings of the same "use Edit not Write" rule.
    edit_write_variants = [
        "no please use Edit not Write",
        "use Edit instead of Write",
        "stop using Write, use Edit",
        "actually, use Edit not Write here",
        "don't use Write — use Edit",
    ]
    for i, content in enumerate(edit_write_variants):
        _insert_extraction(
            conn,
            kind="correction",
            content=content,
            session_id="s1",
            cwd=cwd,
            ts=base + 10 + i,
            source_uuid=f"ew-{i}",
        )

    # 2 paraphrasings of a "no provider-x" rule.
    provider_x_variants = [
        "no provider-x, use provider-y",
        "stop using provider-x, use provider-y",
    ]
    for i, content in enumerate(provider_x_variants):
        _insert_extraction(
            conn,
            kind="correction",
            content=content,
            session_id="s1",
            cwd=cwd,
            ts=base + 50 + i,
            source_uuid=f"h-{i}",
        )

    conn.commit()
    conn.close()

    s = M.summary(db_path, since="7d")
    tc = s["top_corrections"]
    assert tc, "expected at least one top_corrections entry"
    # The 5-variant Edit/Write group should be #1 with count=5.
    assert tc[0]["count"] == 5
    assert "edit" in tc[0]["normalized"] and "write" in tc[0]["normalized"]
    # The example content should be one of the original variants (and
    # specifically the longest one — more context for the user).
    assert tc[0]["content"] in edit_write_variants
    assert tc[0]["content"] == max(edit_write_variants, key=len)
    # The 2-variant provider-x group should be #2 with count=2.
    assert len(tc) >= 2
    assert tc[1]["count"] == 2
    assert "provider" in tc[1]["normalized"]


def test_health_graceful_on_v1_schema(v1_db: Path) -> None:
    h = M.health(v1_db)
    assert h["total_messages"] == 1
    assert h["total_extractions"] == 1
    assert h["total_turns"] == 0
    assert h["total_compactions"] == 0
    assert h["ingest_runs_7d"] == 0
    assert h["last_ingest_ts"] is None
    assert h["last_ingest_age_seconds"] is None
    assert h["error_count_7d"] == 0
    assert h["p50_ingest_ms"] == 0
    assert h["db_size_bytes"] > 0
