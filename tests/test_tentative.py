"""Unit tests for :mod:`index.tentative`.

Covers:
  * schema is idempotent
  * ``add_tentative`` inserts then bumps ``evidence_count`` on dedup
  * ``promote_eligible`` graduates rows past the threshold with adequate
    time-spread; sub-threshold rows stay put
  * ``drop_expired`` marks stale rows so a follow-up ``promote_eligible``
    no longer sees them
  * supersession-columns migration is idempotent and only touches tables
    that already exist
"""

from __future__ import annotations

import sqlite3

import pytest

from index.tentative import (
    SCHEMA_SQL,
    add_supersession_columns,
    add_tentative,
    drop_expired,
    ensure_schema,
    promote_eligible,
)

HOUR = 3600
DAY = 86400


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_ensure_schema_is_idempotent(db):
    # Calling ensure_schema twice in a row must not raise.
    ensure_schema(db)
    ensure_schema(db)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(tentative_facts)")}
    assert {
        "id",
        "key",
        "value",
        "source_kind",
        "source_uuid",
        "evidence_count",
        "first_seen_ts",
        "last_seen_ts",
        "promoted_at",
        "dropped_at",
    }.issubset(cols)


def test_schema_sql_is_module_constant():
    # Cheap sanity: the SCHEMA_SQL string really creates the expected table.
    assert "tentative_facts" in SCHEMA_SQL
    assert "UNIQUE(key, value)" in SCHEMA_SQL


# ---------------------------------------------------------------------------
# add_tentative
# ---------------------------------------------------------------------------


def test_add_tentative_inserts_once(db):
    add_tentative(db, "email_primary", "x@y", "inferred_from_mention", "uuid-1", ts=1000)
    rows = db.execute("SELECT * FROM tentative_facts").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "email_primary"
    assert row["value"] == "x@y"
    assert row["evidence_count"] == 1
    assert row["first_seen_ts"] == 1000
    assert row["last_seen_ts"] == 1000


def test_add_tentative_bumps_count_on_repeat(db):
    add_tentative(db, "email_primary", "x@y", "inferred_from_mention", "u1", ts=1000)
    add_tentative(db, "email_primary", "x@y", "inferred_from_mention", "u2", ts=2000)
    add_tentative(db, "email_primary", "x@y", "explicit_user_assertion", "u3", ts=3000)
    row = db.execute("SELECT * FROM tentative_facts WHERE key='email_primary'").fetchone()
    assert row["evidence_count"] == 3
    assert row["first_seen_ts"] == 1000  # first_seen is frozen
    assert row["last_seen_ts"] == 3000  # last_seen tracks the newest
    # Most-recent citation wins so a downstream promotion has the freshest source.
    assert row["source_kind"] == "explicit_user_assertion"
    assert row["source_uuid"] == "u3"


def test_add_tentative_distinct_values_are_separate_rows(db):
    add_tentative(db, "default_provider", "provider-y", "inferred_from_mention", "u1", ts=1)
    add_tentative(db, "default_provider", "provider-x", "inferred_from_mention", "u2", ts=2)
    n = db.execute("SELECT COUNT(*) FROM tentative_facts").fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------------------
# promote_eligible
# ---------------------------------------------------------------------------


def test_promote_eligible_requires_threshold(db):
    # Single mention, even with wide time spread, doesn't promote.
    add_tentative(db, "k", "v", "explicit_user_assertion", "u1", ts=1_000_000)
    promoted = promote_eligible(db, threshold=2, ttl_days=30, now_ts=1_000_000 + HOUR)
    assert promoted == []


def test_promote_eligible_requires_time_spread(db):
    # Two mentions within seconds → not enough spread → still tentative.
    add_tentative(db, "k", "v", "inferred_from_mention", "u1", ts=1_000_000)
    add_tentative(db, "k", "v", "inferred_from_mention", "u2", ts=1_000_005)
    promoted = promote_eligible(db, threshold=2, ttl_days=30, min_spread_s=HOUR, now_ts=1_000_010)
    assert promoted == []


def test_promote_eligible_graduates_when_both_met(db):
    add_tentative(db, "k", "v", "inferred_from_mention", "u1", ts=1_000_000)
    add_tentative(db, "k", "v", "inferred_from_mention", "u2", ts=1_000_000 + 2 * HOUR)
    promoted = promote_eligible(
        db, threshold=2, ttl_days=30, min_spread_s=HOUR, now_ts=1_000_000 + 3 * HOUR
    )
    assert len(promoted) == 1
    p = promoted[0]
    assert p["key"] == "k"
    assert p["value"] == "v"
    assert p["evidence_count"] == 2

    # And the row is marked promoted so a second call doesn't re-emit it.
    again = promote_eligible(
        db, threshold=2, ttl_days=30, min_spread_s=HOUR, now_ts=1_000_000 + 4 * HOUR
    )
    assert again == []
    row = db.execute("SELECT promoted_at FROM tentative_facts WHERE key='k'").fetchone()
    assert row["promoted_at"] is not None


def test_promote_eligible_drops_expired_facts(db):
    # Old, never reached threshold → should be dropped by ttl sweep.
    add_tentative(db, "stale", "v", "inferred_from_mention", "u1", ts=1_000_000)
    far_future = 1_000_000 + 31 * DAY
    promoted = promote_eligible(db, threshold=2, ttl_days=30, now_ts=far_future)
    assert promoted == []
    row = db.execute(
        "SELECT promoted_at, dropped_at FROM tentative_facts WHERE key='stale'"
    ).fetchone()
    assert row["promoted_at"] is None
    assert row["dropped_at"] == far_future


def test_promote_eligible_ignores_already_promoted_rows(db):
    # A previously-promoted row whose evidence keeps growing must not be
    # re-emitted on the next sweep.
    add_tentative(db, "k", "v", "inferred_from_mention", "u1", ts=1_000_000)
    add_tentative(db, "k", "v", "inferred_from_mention", "u2", ts=1_000_000 + 2 * HOUR)
    promote_eligible(db, threshold=2, min_spread_s=HOUR, now_ts=1_000_000 + 3 * HOUR)
    add_tentative(db, "k", "v", "inferred_from_mention", "u3", ts=1_000_000 + 4 * HOUR)
    again = promote_eligible(db, threshold=2, min_spread_s=HOUR, now_ts=1_000_000 + 5 * HOUR)
    assert again == []


# ---------------------------------------------------------------------------
# drop_expired
# ---------------------------------------------------------------------------


def test_drop_expired_only_touches_old_rows(db):
    # Two rows, one old, one fresh.
    add_tentative(db, "old", "v", "inferred_from_mention", "u1", ts=1_000_000)
    add_tentative(db, "fresh", "v", "inferred_from_mention", "u2", ts=2_000_000)
    n_dropped = drop_expired(db, ttl_days=10, now_ts=2_000_000)
    assert n_dropped == 1
    rows = {
        r["key"]: r["dropped_at"] for r in db.execute("SELECT key, dropped_at FROM tentative_facts")
    }
    assert rows["old"] == 2_000_000
    assert rows["fresh"] is None


def test_drop_expired_is_idempotent(db):
    add_tentative(db, "k", "v", "inferred_from_mention", "u1", ts=1_000_000)
    drop_expired(db, ttl_days=10, now_ts=1_000_000 + 30 * DAY)
    # A second call shouldn't re-drop the same row (rowcount = 0).
    n2 = drop_expired(db, ttl_days=10, now_ts=1_000_000 + 31 * DAY)
    assert n2 == 0


# ---------------------------------------------------------------------------
# add_supersession_columns — migration
# ---------------------------------------------------------------------------


def test_add_supersession_columns_adds_to_existing_tables(db):
    # Stand in for the real profile tables; the migration should treat any
    # existing table by name the same way.
    db.executescript(
        """
        CREATE TABLE operator_profile (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE bans (
            id INTEGER PRIMARY KEY,
            banned_thing TEXT
        );
        """
    )
    result = add_supersession_columns(db, tables=("operator_profile", "bans", "nonexistent_table"))

    op_cols = {r["name"] for r in db.execute("PRAGMA table_info(operator_profile)")}
    ban_cols = {r["name"] for r in db.execute("PRAGMA table_info(bans)")}
    for col in ("superseded_at", "superseded_by_id", "evidence_count"):
        assert col in op_cols
        assert col in ban_cols

    # Migration reports which columns it actually added.
    assert set(result["operator_profile"]) == {
        "superseded_at",
        "superseded_by_id",
        "evidence_count",
    }
    assert set(result["bans"]) == {"superseded_at", "superseded_by_id", "evidence_count"}
    # And it silently skips tables that don't exist.
    assert "nonexistent_table" not in result


def test_add_supersession_columns_is_idempotent(db):
    db.executescript("CREATE TABLE operator_profile (key TEXT PRIMARY KEY)")
    add_supersession_columns(db, tables=("operator_profile",))
    # Second call should add zero columns (no error, no duplicates).
    second = add_supersession_columns(db, tables=("operator_profile",))
    assert second["operator_profile"] == []
