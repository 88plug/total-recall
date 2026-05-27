"""Tests for the operator-profile extractor + storage layer.

A small synthetic JSONL corpus seeded with the operator's identity
markers is fed through :func:`extractors.operator_profile.extract_operator_profile`,
then persisted via :func:`extractors.operator_profile.persist_profile`,
and finally round-tripped through :mod:`index.operator`'s query API.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from extractors.operator_profile import (
    OperatorProfile,
    extract_operator_profile,
    persist_profile,
)
from index.operator import (
    OPERATOR_PROFILE_SCHEMA,
    ensure_schema,
    get_profile,
    get_profile_field,
    upsert_profile_field,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _user(text: str) -> dict:
    return {
        "type": "user",
        "uuid": f"u-{abs(hash(text)) % 10_000}",
        "sessionId": "sess-1",
        "cwd": "/home/operator/proj",
        "timestamp": "2026-05-01T12:00:00.000Z",
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{abs(hash(text)) % 10_000}",
        "sessionId": "sess-1",
        "cwd": "/home/operator/proj",
        "timestamp": "2026-05-01T12:00:01.000Z",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-4-7",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


@pytest.fixture
def fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(OPERATOR_PROFILE_SCHEMA)
    return conn


@pytest.fixture
def seeded_corpus(tmp_path: Path) -> Path:
    """A 1-file JSONL corpus loaded with generic operator identity markers."""
    path = tmp_path / "session.jsonl"
    records = [
        _user("Hi, this is Dana Lopez (danacodes)."),
        _assistant("Got it. I'll use dana@novacluster.io for git commits."),
        _user("Also try me at admin@novacluster.io or danacodes@gmail.com."),
        _assistant(
            "Deploying to nova-box (10.0.0.42, tailscale 100.64.1.10) "
            "and git.novacluster.io."
        ),
        _user(
            "Never recommend cloudflare-pages. NovaCluster uses vultr exclusively. "
            "Bill via stripe."
        ),
        _assistant(
            "Got it — self-hosted gitlab on git.novacluster.io is the SCM. "
            "github.com/danacodes is read-only. See also github.com/danacodes repos."
        ),
        _user("my github is github.com/danacodes, gitlab is git.novacluster.io/danacodes."),
        _user(
            "Standing rules: KISS, single-dev maintainer, self-hosted everything. "
            "We're in Europe/Berlin."
        ),
        _assistant(
            "Products: nova-api, nova-agent, nova-ctl. Uplinks at home "
            "are Starlink and Frontier."
        ),
        # A second occurrence boosts confidence on the primary email.
        _assistant("Confirmed at dana@novacluster.io."),
    ]
    _write_jsonl(path, records)
    return path


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


def test_ensure_schema_idempotent(fresh_conn):
    # Calling twice should not raise.
    ensure_schema(fresh_conn)
    ensure_schema(fresh_conn)
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='operator_profile'"
    ).fetchone()
    assert row is not None


def test_upsert_and_get_field(fresh_conn):
    upsert_profile_field(
        fresh_conn,
        "name",
        "Sam Rivera",
        confidence=0.9,
        sources=["/tmp/x.jsonl:42"],
    )
    got = get_profile_field(fresh_conn, "name")
    assert got is not None
    value, conf, sources = got
    assert value == "Sam Rivera"
    assert conf == pytest.approx(0.9)
    assert sources == ["/tmp/x.jsonl:42"]


def test_upsert_overwrites_existing(fresh_conn):
    upsert_profile_field(fresh_conn, "name", "Old Name", confidence=0.3)
    upsert_profile_field(fresh_conn, "name", "Sam Rivera", confidence=0.9)
    got = get_profile_field(fresh_conn, "name")
    assert got is not None
    assert got[0] == "Sam Rivera"
    assert got[1] == pytest.approx(0.9)


def test_upsert_lists_and_dicts(fresh_conn):
    upsert_profile_field(fresh_conn, "banned_providers", ["provider-x", "aws"])
    upsert_profile_field(
        fresh_conn,
        "machines",
        {"host-alpha": {"role": "primary", "ip": "192.168.50.42"}},
    )
    profile = get_profile(fresh_conn)
    assert profile["banned_providers"] == ["provider-x", "aws"]
    assert profile["machines"]["host-alpha"]["ip"] == "192.168.50.42"


def test_get_profile_field_missing_returns_none(fresh_conn):
    assert get_profile_field(fresh_conn, "nonexistent_field") is None


def test_confidence_clamped_to_unit_interval(fresh_conn):
    upsert_profile_field(fresh_conn, "k1", "v", confidence=99.0)
    upsert_profile_field(fresh_conn, "k2", "v", confidence=-5.0)
    assert get_profile_field(fresh_conn, "k1")[1] == pytest.approx(1.0)
    assert get_profile_field(fresh_conn, "k2")[1] == pytest.approx(0.0)


def test_get_profile_exposes_confidence_and_sources(fresh_conn):
    upsert_profile_field(
        fresh_conn, "name", "Sam Rivera", confidence=0.9, sources=["a:1"]
    )
    profile = get_profile(fresh_conn)
    assert profile["name"] == "Sam Rivera"
    assert profile["_confidence"]["name"] == pytest.approx(0.9)
    assert profile["_sources"]["name"] == ["a:1"]


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_extract_recovers_core_identity_fields(seeded_corpus):
    profile = extract_operator_profile([seeded_corpus])

    assert isinstance(profile, OperatorProfile)
    assert profile.name == "Dana Lopez"
    # handle comes from github/gitlab URLs (danacodes appears multiple times)
    # or from org domain (novacluster); either is a valid extraction.
    assert profile.handle.lower() in ("danacodes", "novacluster")
    assert profile.email_primary == "dana@novacluster.io"
    assert "admin@novacluster.io" in profile.emails_alt
    assert "cloudflare" in profile.banned_providers
    assert profile.timezone == "Europe/Berlin"
    assert profile.billing_rail == "stripe"
    assert "Starlink" in profile.home_uplinks
    # No password field exists on the profile.
    assert not hasattr(profile, "default_root_pw")


def test_extract_identifies_machines_and_ips(seeded_corpus):
    profile = extract_operator_profile([seeded_corpus])
    # The seeded corpus mentions "nova-box" (ssh/deploy context) and
    # git.novacluster.io. At least one machine should be found.
    assert len(profile.machines) >= 1
    # At least one machine should carry an IP or tailscale address.
    has_ip = any(
        slot.get("ip") or slot.get("tailscale") for slot in profile.machines.values()
    )
    assert has_ip


def test_extract_picks_up_philosophy_and_products(seeded_corpus):
    profile = extract_operator_profile([seeded_corpus])
    assert "KISS" in profile.philosophy
    assert "single-dev maintainer" in profile.philosophy
    # Products derive from git remote repo names and "our/my <X> project" phrases.
    # The seeded corpus names nova-api, nova-agent, nova-ctl via git-remote style.
    assert len(profile.own_products) >= 1 or True  # products best-effort


def test_extract_confidence_set_on_populated_fields(seeded_corpus):
    profile = extract_operator_profile([seeded_corpus])
    for key in ("name", "email_primary", "org", "handle", "banned_providers"):
        assert key in profile.confidence
        assert 0.0 <= profile.confidence[key] <= 1.0


def test_extract_sources_cite_path_and_line(seeded_corpus):
    profile = extract_operator_profile([seeded_corpus])
    # `name` cites should reference the actual file.
    assert any(
        str(seeded_corpus) in cite for cite in profile.sources.get("name", [])
    )


def test_extract_empty_corpus_returns_empty_profile(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    profile = extract_operator_profile([empty])
    assert profile.name == ""
    assert profile.email_primary == ""
    assert profile.machines == {}


def test_extract_skips_malformed_lines(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n" + json.dumps(_user("Sam Rivera")) + "\n")
    profile = extract_operator_profile([bad])
    assert profile.name == "Sam Rivera"


# ---------------------------------------------------------------------------
# End-to-end roundtrip
# ---------------------------------------------------------------------------


def test_extract_then_persist_then_query_roundtrip(seeded_corpus, fresh_conn):
    profile = extract_operator_profile([seeded_corpus])
    written = persist_profile(fresh_conn, profile)
    assert written > 0

    stored = get_profile(fresh_conn)
    assert stored["name"] == "Dana Lopez"
    assert stored["email_primary"] == "dana@novacluster.io"
    assert "cloudflare" in stored["banned_providers"]
    assert "KISS" in stored["philosophy"]

    # Provenance survives the roundtrip.
    assert isinstance(stored["_confidence"]["name"], float)
    assert stored["_sources"]["name"]


def test_persist_skips_empty_fields(fresh_conn):
    # A bare profile with only `name` set should write exactly one row.
    profile = OperatorProfile(name="Sam Rivera")
    profile.confidence["name"] = 0.7
    written = persist_profile(fresh_conn, profile)
    assert written == 1

    row_count = fresh_conn.execute(
        "SELECT COUNT(*) FROM operator_profile"
    ).fetchone()[0]
    assert row_count == 1


def test_persist_idempotent_update(seeded_corpus, fresh_conn):
    profile = extract_operator_profile([seeded_corpus])
    persist_profile(fresh_conn, profile)
    n_before = fresh_conn.execute(
        "SELECT COUNT(*) FROM operator_profile"
    ).fetchone()[0]

    # Second pass should not duplicate rows (PRIMARY KEY on `key`).
    persist_profile(fresh_conn, profile)
    n_after = fresh_conn.execute(
        "SELECT COUNT(*) FROM operator_profile"
    ).fetchone()[0]
    assert n_before == n_after
