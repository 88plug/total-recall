"""Regression tests for the extraction -> structured-lookup-table projection.

The extractors emit ``ban`` / ``standing_decision`` / ``failed_attempt`` /
``goal`` rows into ``extractions`` with a fully-populated ``context_json``
payload whose keys already match the corresponding writer kwargs. Before
this module's fix nothing ever called those writers, so ``bans``,
``standing_decisions``, ``failed_attempts`` and ``goal_stack`` stayed empty
on a real corpus and every structured MCP tool (``check_banned``,
``list_standing_decisions``, ``get_active_goal``, ``list_failed_attempts``)
returned a false negative.

These tests drive :func:`index.ingest._commit_parsed` — the single commit
seam every ingest path funnels through — and assert the projection lands.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from index.ingest import _commit_parsed, _ParsedFile  # noqa: E402

SESSION = "ses_projection_test"
CWD = "/home/andrew/projection-test"
TS = 1_780_000_000


def _extraction_row(
    kind: str,
    content: str,
    context: dict,
    *,
    source_uuid: str,
    ts: int = TS,
    cwd: str = CWD,
) -> tuple:
    """Build one ``extractions`` INSERT tuple (v5 / 11-column shape)."""
    return (
        kind,
        content,
        SESSION,
        cwd,
        ts,
        source_uuid,
        0.8,
        "project",
        json.dumps(context, ensure_ascii=False),
        "claude_code",
        cwd,
    )


def _parsed(*rows: tuple) -> _ParsedFile:
    return _ParsedFile(
        source_file=f"/tmp/{SESSION}.jsonl",
        inode=1,
        size=4096,
        mtime=TS,
        rotated=False,
        start_offset=0,
        extraction_rows=list(rows),
        last_session_id=SESSION,
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# One test per projection
# ---------------------------------------------------------------------------


def test_ban_extraction_projects_into_bans(tmp_db: sqlite3.Connection) -> None:
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "ban",
                "NEVER mention Claude in git ops",
                {
                    "banned_thing": "claude",
                    "ban_strength": "absolute",
                    "ban_text": "NEVER mention Claude in git ops",
                },
                source_uuid="msg_ban_1",
            )
        ),
    )

    rows = tmp_db.execute("SELECT * FROM bans").fetchall()
    assert len(rows) == 1, "ban extraction did not project into the bans table"
    assert rows[0]["banned_thing"] == "claude"
    assert rows[0]["ban_strength"] == "absolute"

    from index.bans import check_banned

    hit = check_banned(tmp_db, "claude")
    assert hit is not None, "check_banned must find a projected ban"


def test_standing_decision_extraction_projects(tmp_db: sqlite3.Connection) -> None:
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "standing_decision",
                "provider-a over provider-b",
                {
                    "topic": "cloud_provider",
                    "chose": "provider-a",
                    "over": "provider-b",
                    "rationale": "cheaper GPUs",
                    "scope": "project",
                    "pattern": "preference",
                },
                source_uuid="msg_dec_1",
            )
        ),
    )

    rows = tmp_db.execute("SELECT * FROM standing_decisions").fetchall()
    assert len(rows) == 1, "standing_decision extraction did not project"
    assert rows[0]["topic"] == "cloud_provider"
    assert rows[0]["chose"] == "provider-a"
    assert rows[0]["over"] == "provider-b"


def test_failed_attempt_extraction_projects(tmp_db: sqlite3.Connection) -> None:
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "failed_attempt",
                "wedge",
                {"attempt": "wedge", "reason": "crashed all 6 serving nodes"},
                source_uuid="msg_fa_1",
            )
        ),
    )

    rows = tmp_db.execute("SELECT * FROM failed_attempts").fetchall()
    assert len(rows) == 1, "failed_attempt extraction did not project"
    assert rows[0]["attempt"] == "wedge"
    assert "crashed" in (rows[0]["reason"] or "")


def test_goal_extraction_projects_into_goal_stack(tmp_db: sqlite3.Connection) -> None:
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "goal",
                "Wire the structured projections end to end",
                {"source": "user_string", "first_message": True, "marker": False},
                source_uuid="msg_goal_1",
            )
        ),
    )

    rows = tmp_db.execute("SELECT * FROM goal_stack").fetchall()
    assert len(rows) == 1, "goal extraction did not project into goal_stack"
    assert "structured projections" in rows[0]["goal_text"]


# ---------------------------------------------------------------------------
# Behaviour guarantees
# ---------------------------------------------------------------------------


def test_projection_is_idempotent(tmp_db: sqlite3.Connection) -> None:
    """Re-committing the same parsed file must not duplicate lookup rows."""
    rows = (
        _extraction_row(
            "ban",
            "no pytorch",
            {"banned_thing": "pytorch", "ban_strength": "absolute", "ban_text": "no pytorch"},
            source_uuid="msg_idem_ban",
        ),
        _extraction_row(
            "standing_decision",
            "uv over pip",
            {"topic": "python_installer", "chose": "uv", "over": "pip", "scope": "global"},
            source_uuid="msg_idem_dec",
        ),
    )
    _commit_parsed(tmp_db, _parsed(*rows))
    _commit_parsed(tmp_db, _parsed(*rows))

    assert _count(tmp_db, "bans") == 1
    assert _count(tmp_db, "standing_decisions") == 1


def test_ban_thing_trailing_punctuation_collapses(tmp_db: sqlite3.Connection) -> None:
    """``claude`` and ``claude.`` are the same ban, not two rows.

    Real corpus split the operator's single no-Claude rule 99/10 across the
    two spellings because the normalizer only stripped whitespace + case.
    """
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "ban",
                "never claude",
                {"banned_thing": "claude", "ban_strength": "absolute", "ban_text": "never claude"},
                source_uuid="msg_p1",
            ),
            _extraction_row(
                "ban",
                "never claude.",
                {
                    "banned_thing": "claude.",
                    "ban_strength": "absolute",
                    "ban_text": "never claude.",
                },
                source_uuid="msg_p2",
            ),
        ),
    )

    assert _count(tmp_db, "bans") == 1, "punctuation variants must collapse to one ban"
    assert tmp_db.execute("SELECT banned_thing FROM bans").fetchone()[0] == "claude"


def test_bulk_load_skips_projection(tmp_db: sqlite3.Connection) -> None:
    """``update_profiles=False`` (rebuild bulk path) must not project."""
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "ban",
                "no bulk",
                {"banned_thing": "bulk", "ban_strength": "absolute", "ban_text": "no bulk"},
                source_uuid="msg_bulk",
            )
        ),
        update_profiles=False,
    )
    assert _count(tmp_db, "bans") == 0


def test_malformed_context_never_fails_ingest(tmp_db: sqlite3.Connection) -> None:
    """A junk payload is skipped; the extraction itself still commits."""
    row = (
        "ban",
        "broken",
        SESSION,
        CWD,
        TS,
        "msg_broken",
        0.8,
        "project",
        "{not valid json",
        "claude_code",
        CWD,
    )
    report = _commit_parsed(tmp_db, _parsed(row))
    assert report.new_extractions == 1
    assert _count(tmp_db, "bans") == 0


def test_ban_missing_required_field_is_skipped(tmp_db: sqlite3.Connection) -> None:
    """No ``banned_thing`` -> nothing to key on -> skip, don't crash."""
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "ban",
                "vague",
                {"ban_strength": "absolute", "ban_text": "vague"},
                source_uuid="msg_nothing",
            )
        ),
    )
    assert _count(tmp_db, "bans") == 0


# ---------------------------------------------------------------------------
# One-time backfill for indexes built before the projection existed
# ---------------------------------------------------------------------------


def _seed_legacy_extraction(conn: sqlite3.Connection) -> None:
    """Insert an extraction the way a pre-fix ingest would have: no projection."""
    conn.execute(
        """
        INSERT INTO extractions(
            kind, content, session_id, cwd, ts, source_uuid,
            score, scope, context_json, source, project_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _extraction_row(
            "ban",
            "legacy ban",
            {
                "banned_thing": "legacy",
                "ban_strength": "absolute",
                "ban_text": "legacy ban",
            },
            source_uuid="msg_legacy",
        ),
    )
    conn.commit()


def test_backfill_projects_preexisting_extractions(tmp_db: sqlite3.Connection) -> None:
    from index.ingest import backfill_structured

    _seed_legacy_extraction(tmp_db)
    assert _count(tmp_db, "bans") == 0

    assert backfill_structured(tmp_db) is True
    assert _count(tmp_db, "bans") == 1


def test_backfill_runs_once(tmp_db: sqlite3.Connection) -> None:
    """The sentinel stops the sweep repeating on every ingest tick."""
    from index.ingest import backfill_structured

    _seed_legacy_extraction(tmp_db)
    assert backfill_structured(tmp_db) is True
    assert backfill_structured(tmp_db) is False
    assert backfill_structured(tmp_db, force=True) is True
    assert _count(tmp_db, "bans") == 1


def test_commit_triggers_backfill_for_legacy_rows(tmp_db: sqlite3.Connection) -> None:
    """A normal ingest tick heals a legacy index without a full rebuild."""
    _seed_legacy_extraction(tmp_db)
    assert _count(tmp_db, "bans") == 0

    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "ban",
                "fresh ban",
                {
                    "banned_thing": "fresh",
                    "ban_strength": "absolute",
                    "ban_text": "fresh ban",
                },
                source_uuid="msg_fresh",
            )
        ),
    )

    things = {r[0] for r in tmp_db.execute("SELECT banned_thing FROM bans")}
    assert things == {"legacy", "fresh"}


# ---------------------------------------------------------------------------
# Versioned operator-profile re-mine
# ---------------------------------------------------------------------------


def _seed_stale_profile(conn: sqlite3.Connection) -> None:
    """Store a profile the way the pre-corroboration rules would have.

    Plus the operator text that lets a re-mine reach the right answer.
    """
    from index.operator import ensure_schema as _profile_schema

    _profile_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO operator_profile(key, value) VALUES ('name', ?)",
        ('"The Monitor"',),
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO messages(
            session_id, cwd, role, ts, byte_offset, source_file, message_uuid, text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (SESSION, CWD, "user", TS, i, "/tmp/s.jsonl", f"u{i}", t)
            for i, t in enumerate(
                [
                    "reach me at dana@example.com",
                    "Dana Lopez owns this repo",
                    "I'll wait on the Monitor for readiness",
                ]
            )
        ],
    )
    conn.commit()


def _profile_name(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM operator_profile WHERE key='name'").fetchone()
    return row[0] if row else None


def test_remine_replaces_a_profile_built_by_old_rules(tmp_db: sqlite3.Connection) -> None:
    from index.ingest import remine_profile

    _seed_stale_profile(tmp_db)
    assert "The Monitor" in (_profile_name(tmp_db) or "")

    assert remine_profile(tmp_db) is True
    assert "The Monitor" not in (_profile_name(tmp_db) or "")
    assert "Dana" in (_profile_name(tmp_db) or "")


def test_remine_is_keyed_on_the_rules_version(tmp_db: sqlite3.Connection) -> None:
    """Runs once per rules version, not once ever — the next fix self-heals."""
    from index.ingest import (
        _PROFILE_REMINE_FLAG,
        _PROFILE_RULES_VERSION,
        remine_profile,
    )

    _seed_stale_profile(tmp_db)
    assert remine_profile(tmp_db) is True
    assert remine_profile(tmp_db) is False, "same version must not re-run"

    # Simulate shipping a newer ruleset.
    tmp_db.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
        (_PROFILE_REMINE_FLAG, str(_PROFILE_RULES_VERSION - 1)),
    )
    tmp_db.commit()
    assert remine_profile(tmp_db) is True, "older stored version must re-mine"


def test_remine_tolerates_a_corrupt_sentinel(tmp_db: sqlite3.Connection) -> None:
    from index.ingest import _PROFILE_REMINE_FLAG, remine_profile

    _seed_stale_profile(tmp_db)
    tmp_db.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, 'not-a-number')",
        (_PROFILE_REMINE_FLAG,),
    )
    tmp_db.commit()
    assert remine_profile(tmp_db) is True


def test_commit_triggers_the_remine(tmp_db: sqlite3.Connection) -> None:
    """A normal ingest tick heals the profile without a rebuild."""
    _seed_stale_profile(tmp_db)
    _commit_parsed(
        tmp_db,
        _parsed(
            _extraction_row(
                "ban",
                "no junk",
                {"banned_thing": "junk", "ban_strength": "absolute", "ban_text": "no junk"},
                source_uuid="msg_remine_tick",
            )
        ),
    )
    assert "The Monitor" not in (_profile_name(tmp_db) or "")


def test_remine_skips_a_fresh_index_but_stamps_the_version(
    tmp_db: sqlite3.Connection,
) -> None:
    """A fresh index has no stale profile — re-mining would poison it.

    Without this guard the first commit mines the one file it just wrote,
    persists that as the authoritative profile, and stamps the version so the
    real sweep never runs.
    """
    from index.ingest import _PROFILE_REMINE_FLAG, _PROFILE_RULES_VERSION, remine_profile
    from index.operator import ensure_schema

    ensure_schema(tmp_db)
    assert tmp_db.execute("SELECT count(*) FROM operator_profile").fetchone()[0] == 0

    assert remine_profile(tmp_db) is False, "nothing to repair on a fresh index"
    assert tmp_db.execute("SELECT count(*) FROM operator_profile").fetchone()[0] == 0

    row = tmp_db.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (_PROFILE_REMINE_FLAG,)
    ).fetchone()
    assert row is not None and int(row[0]) == _PROFILE_RULES_VERSION
