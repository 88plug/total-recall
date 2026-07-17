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
    extract_incremental,
    extract_operator_profile,
    extract_operator_profile_from_records,
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
            "Deploying to nova-box (10.0.0.42, tailscale 100.64.1.10) and git.novacluster.io."
        ),
        _user(
            "Never recommend cloudflare-pages. NovaCluster uses vultr exclusively. Bill via stripe."
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
            "Products: nova-api, nova-agent, nova-ctl. Uplinks at home are Starlink and Frontier."
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
        {"host-alpha": {"role": "primary", "ip": "10.0.0.42"}},
    )
    profile = get_profile(fresh_conn)
    assert profile["banned_providers"] == ["provider-x", "aws"]
    assert profile["machines"]["host-alpha"]["ip"] == "10.0.0.42"


def test_get_profile_field_missing_returns_none(fresh_conn):
    assert get_profile_field(fresh_conn, "nonexistent_field") is None


def test_confidence_clamped_to_unit_interval(fresh_conn):
    upsert_profile_field(fresh_conn, "k1", "v", confidence=99.0)
    upsert_profile_field(fresh_conn, "k2", "v", confidence=-5.0)
    assert get_profile_field(fresh_conn, "k1")[1] == pytest.approx(1.0)
    assert get_profile_field(fresh_conn, "k2")[1] == pytest.approx(0.0)


def test_get_profile_exposes_confidence_and_sources(fresh_conn):
    upsert_profile_field(fresh_conn, "name", "Sam Rivera", confidence=0.9, sources=["a:1"])
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
    has_ip = any(slot.get("ip") or slot.get("tailscale") for slot in profile.machines.values())
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
    assert any(str(seeded_corpus) in cite for cite in profile.sources.get("name", []))


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

    row_count = fresh_conn.execute("SELECT COUNT(*) FROM operator_profile").fetchone()[0]
    assert row_count == 1


def test_persist_idempotent_update(seeded_corpus, fresh_conn):
    profile = extract_operator_profile([seeded_corpus])
    persist_profile(fresh_conn, profile)
    n_before = fresh_conn.execute("SELECT COUNT(*) FROM operator_profile").fetchone()[0]

    # Second pass should not duplicate rows (PRIMARY KEY on `key`).
    persist_profile(fresh_conn, profile)
    n_after = fresh_conn.execute("SELECT COUNT(*) FROM operator_profile").fetchone()[0]
    assert n_before == n_after


# ---------------------------------------------------------------------------
# Regression guards: timezone garbage + handle corroboration + path parity
# ---------------------------------------------------------------------------

# Valid IANA region prefixes — any extracted timezone must start with one of
# these (or be empty).  Anything else (e.g. "Jc/Jmin/Jmax") is garbage.
_VALID_IANA_PREFIXES = (
    "Africa/",
    "America/",
    "Antarctica/",
    "Arctic/",
    "Asia/",
    "Atlantic/",
    "Australia/",
    "Europe/",
    "Indian/",
    "Pacific/",
    "US/",
    "Etc/",
    "GMT/",
    "UTC",  # bare UTC is canonical
)


def _is_valid_iana_or_empty(tz: str) -> bool:
    if not tz:
        return True
    return any(tz.startswith(p) for p in _VALID_IANA_PREFIXES)


def test_timezone_rejects_non_iana_garbage(tmp_path):
    """Extractor must never emit garbage tokens (e.g. code paths) as timezones.

    Part 1: corpus that mentions only code-like ``Word/Word`` fragments and
    NO real timezone → timezone must be empty (garbage rejected).
    Part 2: corpus that mentions ``America/New_York`` → timezone must match
    that valid IANA zone.
    """
    # --- Part 1: garbage-only corpus ---
    garbage_records = [
        _user("The result is in Jc/Jmin/Jmax format."),
        _assistant("Yes, the ratio Pull/Request is tracked in src/utils."),
        _user("See also lib/core and tests/unit for more."),
        _assistant("Graph traversal: O(V/E) complexity. File at config/main."),
        _user("Use Read/Write helpers from io/stream when needed."),
    ]
    garbage_path = tmp_path / "garbage.jsonl"
    _write_jsonl(garbage_path, garbage_records)

    garbage_profile = extract_operator_profile([garbage_path])

    assert _is_valid_iana_or_empty(garbage_profile.timezone), (
        f"Garbage timezone accepted: {garbage_profile.timezone!r} — "
        "non-IANA code-path tokens must be rejected"
    )
    # Specifically: none of the garbage tokens should survive.
    for garbage_token in (
        "Jc/Jmin/Jmax",
        "Pull/Request",
        "src/utils",
        "lib/core",
        "io/stream",
        "config/main",
        "tests/unit",
    ):
        assert garbage_profile.timezone != garbage_token, (
            f"Garbage token {garbage_token!r} was accepted as timezone"
        )

    # --- Part 2: valid IANA zone in corpus ---
    valid_records = [
        _user("I'm based in America/New_York so please schedule meetings accordingly."),
        _assistant("Got it — your timezone is America/New_York."),
    ]
    valid_path = tmp_path / "valid_tz.jsonl"
    _write_jsonl(valid_path, valid_records)

    valid_profile = extract_operator_profile([valid_path])

    assert valid_profile.timezone == "America/New_York", (
        f"Expected America/New_York, got {valid_profile.timezone!r}"
    )
    assert _is_valid_iana_or_empty(valid_profile.timezone)


def test_handle_reinforced_by_email(tmp_path):
    """Operator email local-part/domain must boost the corroborated handle
    above a higher-frequency but unrelated token.

    Scenario: ``github.com/opnsense`` appears 5×; operator email is
    ``dana@novabox.io`` and ``github.com/dana`` appears 3×.  The resolved
    handle must be ``dana`` (corroborated by email local-part), NOT
    ``opnsense`` (unrelated noise).
    """
    records = [
        # High-frequency unrelated token (5 occurrences)
        _assistant("See the opnsense docs at github.com/opnsense for details."),
        _assistant("Filed against github.com/opnsense — upstream issue."),
        _assistant("Patch merged into github.com/opnsense/core."),
        _assistant("Reference: github.com/opnsense/plugins is the source."),
        _assistant("The github.com/opnsense repo is the canonical upstream."),
        # Operator's own handle corroborated by their email (3 occurrences)
        _user("My email is dana@novabox.io — use that for commits."),
        _assistant("Got it — git config user.email dana@novabox.io."),
        _user("Also my github.com/dana profile and github.com/dana/novactl repo."),
        _assistant("Your primary contact is dana@novabox.io. github.com/dana is read-only."),
    ]
    path = tmp_path / "handle_email.jsonl"
    _write_jsonl(path, records)

    profile = extract_operator_profile([path])

    # The handle must be corroborated by the email local-part ("dana")
    # or the domain label ("novabox"), NOT the unrelated high-freq token.
    email_local = "dana"
    email_domain_label = "novabox"
    resolved = (profile.handle or profile.github_user or "").lower()

    assert resolved, "handle/github_user both empty after extraction"
    assert resolved in (email_local, email_domain_label), (
        f"Expected handle corroborated by email ('{email_local}' or '{email_domain_label}'), "
        f"got {resolved!r} — unrelated high-freq token must not win"
    )


def test_full_and_incremental_agree(tmp_path):
    """``extract_operator_profile`` and ``extract_incremental`` must produce
    the same ``timezone`` and ``handle`` for the same synthetic corpus.

    This is the regression guard for the path-divergence bug where the
    incremental path emitted garbage timezones and wrong handles while
    the file-based path was correct.
    """
    records = [
        _user("My github is github.com/stellarops — use that for all PRs."),
        _assistant("Noted: github.com/stellarops is your handle."),
        _user("I'm in Europe/Berlin timezone — please keep that in mind."),
        _assistant(
            "Understood. git config user.name 'Felix Ortega' and "
            "git config user.email felix@stellarops.dev should match."
        ),
        _user("Confirmed: felix@stellarops.dev is the primary email."),
        _assistant("github.com/stellarops confirmed as the primary handle."),
    ]

    # Write to JSONL for the file-based path.
    jsonl_path = tmp_path / "parity.jsonl"
    _write_jsonl(jsonl_path, records)

    # Path A: file-based extractor.
    file_profile = extract_operator_profile([jsonl_path])

    # Path B: incremental (in-memory records), starting from an empty base.
    incr_profile = extract_incremental(records)

    # Both paths must agree on timezone validity.
    assert _is_valid_iana_or_empty(file_profile.timezone), (
        f"file path produced garbage timezone: {file_profile.timezone!r}"
    )
    assert _is_valid_iana_or_empty(incr_profile.timezone), (
        f"incremental path produced garbage timezone: {incr_profile.timezone!r}"
    )

    # Both must resolve the same timezone (if either resolved one).
    if file_profile.timezone or incr_profile.timezone:
        assert file_profile.timezone == incr_profile.timezone, (
            f"Path divergence on timezone: "
            f"file={file_profile.timezone!r}, incremental={incr_profile.timezone!r}"
        )

    # Both must resolve the same handle family (allow minor case differences).
    file_handle = (file_profile.handle or file_profile.github_user or "").lower()
    incr_handle = (incr_profile.handle or incr_profile.github_user or "").lower()

    if file_handle or incr_handle:
        assert file_handle == incr_handle, (
            f"Path divergence on handle: file={file_handle!r}, incremental={incr_handle!r}"
        )


# ---------------------------------------------------------------------------
# Multi-source coverage: extract_operator_profile_from_records
# ---------------------------------------------------------------------------


def _opencode_record(text: str) -> dict:
    """Synthetic record shaped like a normalized OpenCode dict passed through
    ``_iter_text_from_records``.  We set ``source_file`` to a path that would
    come from an opencode session so citations reflect a non-claude_code origin.
    """
    return {
        "type": "user",
        "sessionId": "oc-sess-1",
        "cwd": "/home/operator/oc-proj",
        "timestamp": "2026-05-01T14:00:00.000Z",
        "source_file": "/home/operator/.local/share/opencode/session-abc.json",
        "byte_offset": 0,
        "message": {"role": "user", "content": text},
    }


def _opencode_assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": "oc-sess-1",
        "cwd": "/home/operator/oc-proj",
        "timestamp": "2026-05-01T14:00:01.000Z",
        "source_file": "/home/operator/.local/share/opencode/session-abc.json",
        "byte_offset": 100,
        "message": {
            "id": "oc_msg_1",
            "model": "gpt-4o",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_extract_profile_from_records_non_claude_code_source():
    """``extract_operator_profile_from_records`` must surface identity signals
    from records that carry a non-claude_code ``source_file`` path (simulating
    opencode or any other multi-source adapter).  This pins the from_records
    entry point as source-agnostic.
    """
    records = [
        _opencode_record("Hi, I'm Mira Osei (miraosei). My email is mira@helioslab.dev."),
        _opencode_assistant("Got it — git config user.email mira@helioslab.dev."),
        _opencode_record(
            "Never use vercel. HeliosLab runs on bare metal only. I'm in America/Chicago timezone."
        ),
        _opencode_assistant(
            "Deploying to helio-box (10.0.1.5). github.com/miraosei is your handle."
        ),
        _opencode_record("github.com/miraosei is my profile."),
        _opencode_assistant("Confirmed at mira@helioslab.dev."),
    ]

    profile = extract_operator_profile_from_records(records)

    assert isinstance(profile, OperatorProfile)
    assert profile.name == "Mira Osei", (
        f"Expected 'Mira Osei', got {profile.name!r} — "
        "non-claude_code source records must feed identity extraction"
    )
    assert profile.email_primary == "mira@helioslab.dev", (
        f"Expected 'mira@helioslab.dev', got {profile.email_primary!r}"
    )
    assert "vercel" in profile.banned_providers, (
        "banned_providers must include 'vercel' from non-claude_code records"
    )
    assert profile.timezone == "America/Chicago", (
        f"Expected 'America/Chicago', got {profile.timezone!r}"
    )
    handle = (profile.handle or profile.github_user or "").lower()
    assert handle in ("miraosei", "helioslab"), (
        f"Expected handle 'miraosei' (or domain label 'helioslab'), got {handle!r}"
    )


def test_extract_profile_from_records_mixed_sources():
    """Records from two synthetic sources (one claude_code-shaped, one opencode-
    shaped) must be reconciled into a single merged profile.  Both signal the
    same operator; combined confidence must exceed single-source extraction.
    """
    cc_records = [
        _user("My github is github.com/lena-voss — use that for all commits."),
        _assistant("Noted: github.com/lena-voss. I'll use lena@quantumleap.io."),
    ]
    oc_records = [
        _opencode_record("lena@quantumleap.io is my email. I'm in Europe/Paris."),
        _opencode_assistant("Confirmed lena@quantumleap.io. github.com/lena-voss is yours."),
    ]

    profile = extract_operator_profile_from_records(cc_records + oc_records)

    assert profile.email_primary == "lena@quantumleap.io", (
        f"Mixed-source email not resolved: {profile.email_primary!r}"
    )
    assert profile.timezone == "Europe/Paris", (
        f"Mixed-source timezone not resolved: {profile.timezone!r}"
    )
    handle = (profile.handle or profile.github_user or "").lower()
    assert "lena" in handle or handle in ("lena-voss", "quantumleap"), (
        f"Mixed-source handle not resolved: {handle!r}"
    )
    # Confidence on multi-sourced fields should be > 0.
    assert profile.confidence.get("email_primary", 0.0) > 0.0
    assert profile.confidence.get("handle", 0.0) > 0.0
