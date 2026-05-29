"""Tests for ``total-recall consolidate``.

Strategy: build the *expected v3* consolidation tables (operator_profile,
standing_decisions, bans, voice_profile, vocabulary, vocab_candidates,
tentative_facts, projects, machines) in a temp SQLite file, then run the
Click command against it. The CLI's own defensive imports cover the case
where ``index.decay`` / ``index.tentative`` / ``index.dream`` aren't built
yet, so we don't need to monkeypatch those for the happy path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from total_recall.__main__ import cli


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


_CONSOLIDATE_SCHEMA = """
CREATE TABLE operator_profile (
    field TEXT PRIMARY KEY,
    value TEXT,
    confidence REAL,
    last_reasserted_ts INTEGER,
    n_reassertions INTEGER DEFAULT 1,
    archived INTEGER DEFAULT 0
);

CREATE TABLE standing_decisions (
    id INTEGER PRIMARY KEY,
    topic TEXT,
    decision TEXT,
    confidence REAL,
    last_reasserted_ts INTEGER,
    n_reassertions INTEGER DEFAULT 1,
    archived INTEGER DEFAULT 0
);

CREATE TABLE bans (
    id INTEGER PRIMARY KEY,
    pattern TEXT,
    confidence REAL,
    last_reasserted_ts INTEGER,
    n_reassertions INTEGER DEFAULT 1,
    archived INTEGER DEFAULT 0
);

CREATE TABLE voice_profile (
    id INTEGER PRIMARY KEY,
    attribute TEXT,
    confidence REAL,
    last_reasserted_ts INTEGER,
    n_reassertions INTEGER DEFAULT 1,
    archived INTEGER DEFAULT 0
);

CREATE TABLE vocabulary (
    id INTEGER PRIMARY KEY,
    term TEXT UNIQUE,
    tier INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.2,
    n_sessions INTEGER DEFAULT 0,
    n_days INTEGER DEFAULT 0,
    last_seen_ts INTEGER,
    last_reasserted_ts INTEGER,
    archived INTEGER DEFAULT 0
);

CREATE TABLE vocab_candidates (
    term TEXT PRIMARY KEY,
    n_sessions INTEGER,
    n_days INTEGER
);

CREATE TABLE tentative_facts (
    id INTEGER PRIMARY KEY,
    field TEXT,
    value TEXT,
    created_ts INTEGER
);

CREATE TABLE projects (cwd TEXT PRIMARY KEY);
CREATE TABLE machines (
    name TEXT PRIMARY KEY,
    last_seen_ts INTEGER
);
"""


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A v3-shaped SQLite with realistic synthetic data for every step."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_CONSOLIDATE_SCHEMA)

    now = int(time.time())
    day = 86400

    # operator_profile: one 90-day-silent, one 100-day-silent → 100d should
    # have strictly lower confidence after decay.
    conn.execute(
        "INSERT INTO operator_profile(field, value, confidence, last_reasserted_ts) "
        "VALUES (?, ?, ?, ?)",
        ("editor", "vim", 0.9, now - 90 * day),
    )
    conn.execute(
        "INSERT INTO operator_profile(field, value, confidence, last_reasserted_ts) "
        "VALUES (?, ?, ?, ?)",
        ("shell", "bash", 0.9, now - 100 * day),
    )
    # A row whose confidence already crashed: should archive on decay.
    # Keep last_reasserted_ts inside the auto-dream cutoff (30 d) so the
    # subsequent cleanup step doesn't immediately delete the archived row
    # before we can observe it.
    conn.execute(
        "INSERT INTO operator_profile(field, value, confidence, last_reasserted_ts) "
        "VALUES (?, ?, ?, ?)",
        ("dead_field", "x", 0.1, now - 20 * day),
    )

    # Archived-and-superseded-for-31-days row: auto-dream should delete.
    conn.execute(
        "INSERT INTO standing_decisions(topic, decision, confidence, "
        "last_reasserted_ts, archived) VALUES (?, ?, ?, ?, 1)",
        ("dead_topic", "old", 0.05, now - 31 * day),
    )

    # Vocab candidate that has crossed tier-0 → tier-1 thresholds.
    conn.execute(
        "INSERT INTO vocab_candidates(term, n_sessions, n_days) VALUES (?, ?, ?)",
        ("relay", 4, 3),
    )
    conn.execute(
        "INSERT INTO vocab_candidates(term, n_sessions, n_days) VALUES (?, ?, ?)",
        ("toosmall", 2, 1),  # below threshold; should NOT promote
    )

    # Tier-1 entry that should graduate to tier-2.
    conn.execute(
        "INSERT INTO vocabulary(term, tier, confidence, n_sessions, n_days, "
        "last_seen_ts, last_reasserted_ts) VALUES (?, 1, 0.4, 8, 20, ?, ?)",
        ("acme-net", now, now),
    )

    # Tentative fact past TTL — should be dropped.
    conn.execute(
        "INSERT INTO tentative_facts(field, value, created_ts) VALUES (?, ?, ?)",
        ("editor", "emacs", now - 31 * day),
    )
    # And a fresh one — should remain.
    conn.execute(
        "INSERT INTO tentative_facts(field, value, created_ts) VALUES (?, ?, ?)",
        ("shell", "zsh", now - 2 * day),
    )

    # Project pointing to a directory that does NOT exist — auto-dream prunes.
    conn.execute("INSERT INTO projects(cwd) VALUES (?)", ("/nonexistent/totally/gone",))
    conn.execute("INSERT INTO projects(cwd) VALUES (?)", (str(tmp_path),))  # exists

    # Machine unseen in 90 days → prune.
    conn.execute(
        "INSERT INTO machines(name, last_seen_ts) VALUES (?, ?)",
        ("old-laptop", now - 90 * day),
    )
    conn.execute(
        "INSERT INTO machines(name, last_seen_ts) VALUES (?, ?)",
        ("daily-driver", now - 1 * day),
    )

    conn.commit()
    conn.close()
    return db


# --------------------------------------------------------------------------- #
# Smoke: help & registration
# --------------------------------------------------------------------------- #


def test_consolidate_help_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "consolidate" in result.output

    result = runner.invoke(cli, ["consolidate", "--help"])
    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output


# --------------------------------------------------------------------------- #
# Dry-run does no writes
# --------------------------------------------------------------------------- #


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(str(db))
    out: dict[str, list[tuple]] = {}
    for tbl in (
        "operator_profile",
        "standing_decisions",
        "vocabulary",
        "vocab_candidates",
        "tentative_facts",
        "projects",
        "machines",
    ):
        out[tbl] = list(conn.execute(f"SELECT * FROM {tbl}").fetchall())
    conn.close()
    return out


def test_consolidate_dry_run_no_writes(seeded_db: Path) -> None:
    before = _snapshot(seeded_db)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--db", str(seeded_db), "consolidate", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    after = _snapshot(seeded_db)
    assert before == after, "dry-run mutated the DB"
    assert "DRY-RUN" in result.output or "dry-run" in result.output.lower()


# --------------------------------------------------------------------------- #
# Decay: 100d-silent < 90d-silent
# --------------------------------------------------------------------------- #


def test_consolidate_decay_orders_by_silence(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(seeded_db))
    conn.row_factory = sqlite3.Row
    row90 = conn.execute(
        "SELECT confidence FROM operator_profile WHERE field='editor'"
    ).fetchone()
    row100 = conn.execute(
        "SELECT confidence FROM operator_profile WHERE field='shell'"
    ).fetchone()
    conn.close()
    assert row100["confidence"] < row90["confidence"], (
        f"100d ({row100['confidence']}) should decay below 90d ({row90['confidence']})"
    )


def test_consolidate_low_confidence_archived(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(str(seeded_db))
    archived = conn.execute(
        "SELECT archived FROM operator_profile WHERE field='dead_field'"
    ).fetchone()
    conn.close()
    assert archived[0] == 1


# --------------------------------------------------------------------------- #
# Vocabulary tier promotion
# --------------------------------------------------------------------------- #


def test_consolidate_vocab_tier0_to_1(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(seeded_db))
    conn.row_factory = sqlite3.Row
    relay = conn.execute("SELECT * FROM vocabulary WHERE term='relay'").fetchone()
    toosmall = conn.execute(
        "SELECT * FROM vocabulary WHERE term='toosmall'"
    ).fetchone()
    acme_net = conn.execute(
        "SELECT * FROM vocabulary WHERE term='acme-net'"
    ).fetchone()
    conn.close()

    assert relay is not None, "tier-0 → 1 candidate should have graduated"
    assert relay["tier"] == 1
    assert abs(relay["confidence"] - 0.4) < 1e-6

    assert toosmall is None, "below-threshold candidate must NOT promote"

    assert acme_net["tier"] == 2
    assert abs(acme_net["confidence"] - 0.85) < 1e-6


# --------------------------------------------------------------------------- #
# Auto Dream-style cleanup
# --------------------------------------------------------------------------- #


def test_consolidate_dream_deletes_superseded(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(str(seeded_db))
    n = conn.execute(
        "SELECT COUNT(*) FROM standing_decisions WHERE topic='dead_topic'"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "31-day-archived row should be deleted"


def test_consolidate_dream_prunes_missing_cwds(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(str(seeded_db))
    rows = [r[0] for r in conn.execute("SELECT cwd FROM projects").fetchall()]
    conn.close()
    assert "/nonexistent/totally/gone" not in rows
    # the existing tmp_path cwd should still be there
    assert any(r != "/nonexistent/totally/gone" for r in rows)


def test_consolidate_dream_prunes_old_machines(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(str(seeded_db))
    rows = [r[0] for r in conn.execute("SELECT name FROM machines").fetchall()]
    conn.close()
    assert "old-laptop" not in rows
    assert "daily-driver" in rows


# --------------------------------------------------------------------------- #
# Tentative reconciliation
# --------------------------------------------------------------------------- #


def test_consolidate_drops_expired_tentative(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(seeded_db), "consolidate"])
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(str(seeded_db))
    rows = list(conn.execute("SELECT field FROM tentative_facts").fetchall())
    conn.close()
    fields = [r[0] for r in rows]
    assert "editor" not in fields, "31-day-old tentative fact should be dropped"
    assert "shell" in fields, "2-day-old tentative fact should remain"


# --------------------------------------------------------------------------- #
# JSON output is valid
# --------------------------------------------------------------------------- #


def test_consolidate_json_valid(seeded_db: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "--db", str(seeded_db), "consolidate", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert "decay" in payload
    assert "vocabulary" in payload
    assert "dream" in payload
    assert payload["db"] == str(seeded_db)


def test_consolidate_no_db_graceful(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--json", "--db", str(tmp_path / "missing.db"), "consolidate"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "db-missing" in payload["skipped"]


# --------------------------------------------------------------------------- #
# Systemd unit sanity (install script + units)
# --------------------------------------------------------------------------- #


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts"


def test_install_script_executable() -> None:
    s = _scripts_dir() / "install-consolidate-timer.sh"
    assert s.exists()
    assert os.access(s, os.X_OK), "install script must be chmod +x"


def test_service_unit_invokes_cli() -> None:
    s = (_scripts_dir() / "total-recall-consolidate.service").read_text()
    assert "total-recall consolidate" in s
    assert "Type=oneshot" in s


def test_timer_unit_weekly() -> None:
    s = (_scripts_dir() / "total-recall-consolidate.timer").read_text()
    assert "OnCalendar=Sun" in s
    assert "WantedBy=timers.target" in s


# --------------------------------------------------------------------------- #
# Multi-source variant
#
# ``total-recall consolidate`` operates purely on the summary tables
# (operator_profile, standing_decisions, bans, voice_profile, vocabulary,
# vocab_candidates, tentative_facts, projects, machines) — it does NOT
# re-read session files or branch on a per-row ``source`` field.  There is
# therefore no source-specific code path inside the consolidate command that
# could silently exclude opencode or other non-claude_code rows.
#
# The test below confirms this by seeding rows that carry an explicit
# ``source='opencode'`` column (added to the mini-schema) and asserting that
# consolidate's decay pass still processes them correctly.  This guards
# against any future refactor that might accidentally filter by source.
# --------------------------------------------------------------------------- #


_CONSOLIDATE_SCHEMA_WITH_SOURCE = (
    _CONSOLIDATE_SCHEMA.rstrip()
    + "\nALTER TABLE operator_profile ADD COLUMN source TEXT DEFAULT 'claude_code';\n"
)


@pytest.fixture
def seeded_db_multi_source(tmp_path: Path) -> Path:
    """Like ``seeded_db`` but operator_profile rows carry a mix of sources."""
    db = tmp_path / "index_ms.db"
    conn = sqlite3.connect(str(db))
    # Build the base schema first, then add the source column.
    conn.executescript(_CONSOLIDATE_SCHEMA)
    conn.execute(
        "ALTER TABLE operator_profile ADD COLUMN source TEXT DEFAULT 'claude_code'"
    )

    now = int(time.time())
    day = 86400

    # Row from claude_code source — 90 days silent.
    conn.execute(
        "INSERT INTO operator_profile(field, value, confidence, last_reasserted_ts, source) "
        "VALUES (?, ?, ?, ?, ?)",
        ("editor", "vim", 0.9, now - 90 * day, "claude_code"),
    )
    # Row from opencode source — 100 days silent.
    conn.execute(
        "INSERT INTO operator_profile(field, value, confidence, last_reasserted_ts, source) "
        "VALUES (?, ?, ?, ?, ?)",
        ("shell", "bash", 0.9, now - 100 * day, "opencode"),
    )
    # Low-confidence row from aider — should be archived.
    conn.execute(
        "INSERT INTO operator_profile(field, value, confidence, last_reasserted_ts, source) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dead_field", "x", 0.1, now - 20 * day, "aider"),
    )

    # Minimal entries to satisfy other consolidate steps.
    conn.execute(
        "INSERT INTO vocab_candidates(term, n_sessions, n_days) VALUES (?, ?, ?)",
        ("relay", 4, 3),
    )

    conn.commit()
    conn.close()
    return db


def test_consolidate_multi_source_rows_processed(seeded_db_multi_source: Path) -> None:
    """Consolidate must apply decay to rows regardless of their ``source``.

    - The opencode row (100 d silent) must decay below the claude_code row (90 d).
    - The aider low-confidence row must be archived.
    This asserts the command is source-agnostic and cannot silently skip
    non-claude_code entries.
    """
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--db", str(seeded_db_multi_source), "consolidate"]
    )
    assert result.exit_code == 0, result.output

    conn = sqlite3.connect(str(seeded_db_multi_source))
    conn.row_factory = sqlite3.Row

    row_cc = conn.execute(
        "SELECT confidence FROM operator_profile WHERE field='editor'"
    ).fetchone()
    row_oc = conn.execute(
        "SELECT confidence FROM operator_profile WHERE field='shell'"
    ).fetchone()
    row_dead = conn.execute(
        "SELECT archived FROM operator_profile WHERE field='dead_field'"
    ).fetchone()
    conn.close()

    assert row_oc["confidence"] < row_cc["confidence"], (
        f"opencode row (100d) should decay below claude_code row (90d): "
        f"opencode={row_oc['confidence']}, claude_code={row_cc['confidence']}"
    )
    assert row_dead["archived"] == 1, (
        "aider-sourced low-confidence row must be archived by decay pass"
    )
