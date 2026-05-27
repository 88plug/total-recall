"""Tests for the ontology extractor + storage layer.

Six tests covering:

1. Project extraction from a synthetic ``~/.claude/projects/<slug>/`` tree.
2. Machine regex picks up ``host-alpha`` / ``git.example.com`` / ``relay-fra-1``
   plus LAN, Tailscale and GPU strings within the ±200-char window.
3. Vocabulary term lookup is case-insensitive and returns the stored
   definition.
4. Vocabulary frequency increments propagate to the stored row.
5. Project upsert is order-independent: a second pass with only a purpose
   does not clobber a previously-stored message_count.
6. End-to-end ``extract_ontology`` + ``persist_ontology`` round-trips
   correctly through the SQLite layer.

Tests use a temp ``~/.claude/projects``-shaped directory and an in-memory
SQLite DB — no real corpus required, runs cleanly in CI.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from extractors.ontology import (
    MachineRecord,
    ProjectRecord,
    _extract_machines_from_text,
    extract_ontology,
    persist_ontology,
    _UNIVERSAL_CLAUDE_CODE_TERMS,
)
from index.ontology import (
    bump_vocabulary_frequency,
    ensure_schema,
    get_project,
    get_term,
    list_machines,
    list_projects,
    upsert_project,
    upsert_vocabulary_term,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _write_session(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def synthetic_projects_root(tmp_path: Path) -> Path:
    """Build a minimal ``~/.claude/projects/`` tree with two project dirs."""
    root = tmp_path / "projects"
    root.mkdir()

    # Project A — Go heavy, mentions host-alpha and the relay fleet.
    a = root / "-home-operator-acme-net"
    a.mkdir()
    sess_a = a / "00000000-0000-0000-0000-0000000000a1.jsonl"
    _write_session(
        sess_a,
        [
            {
                "type": "user",
                "sessionId": "sA",
                "uuid": "u1",
                "timestamp": "2026-05-01T12:00:00Z",
                "cwd": "/home/operator/acme-net",
                "message": {
                    "role": "user",
                    "content": "wire up acme-net-ctrl on host-alpha (192.168.50.42) and fan out via the relay fleet",
                },
            },
            {
                "type": "assistant",
                "sessionId": "sA",
                "uuid": "a1",
                "parentUuid": "u1",
                "timestamp": "2026-05-01T12:00:01Z",
                "cwd": "/home/operator/acme-net",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Editing main.go and api.go on host-alpha. The RTX 3090 24GiB is up. Tailscale IP 100.64.0.10.",
                        }
                    ],
                },
            },
        ],
    )

    # Project B — Python heavy, related project name shares "acme-net".
    b = root / "-home-operator-acme-net-tools"
    b.mkdir()
    sess_b = b / "00000000-0000-0000-0000-0000000000b1.jsonl"
    _write_session(
        sess_b,
        [
            {
                "type": "user",
                "sessionId": "sB",
                "uuid": "u2",
                "timestamp": "2026-05-02T12:00:00Z",
                "cwd": "/home/operator/acme-net-tools",
                "message": {
                    "role": "user",
                    "content": "rewrite parse.py and helpers.py — relay-fra-1 is misbehaving",
                },
            },
            {
                "type": "assistant",
                "sessionId": "sB",
                "uuid": "a2",
                "parentUuid": "u2",
                "timestamp": "2026-05-02T12:00:01Z",
                "cwd": "/home/operator/acme-net-tools",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Patching parse.py — KISS applies."}
                    ],
                },
            },
        ],
    )

    return root


# ---------------------------------------------------------------------------
# 1. Project extraction
# ---------------------------------------------------------------------------


def test_project_extraction_records_slug_and_language(
    synthetic_projects_root: Path,
) -> None:
    snap = extract_ontology(synthetic_projects_root)
    by_slug = {p.cwd: p for p in snap.projects}

    assert "-home-operator-acme-net" in by_slug
    assert "-home-operator-acme-net-tools" in by_slug

    a = by_slug["-home-operator-acme-net"]
    # Go was the dominant extension in project A.
    assert a.primary_language == "go"
    # Purpose comes from the earliest user turn.
    assert "host-alpha" in a.purpose
    # 2 records (1 user + 1 assistant) for this synthetic session.
    assert a.message_count == 2
    # Adjacency picks up sibling via shared "acme-net" token.
    assert "-home-operator-acme-net-tools" in a.related_projects

    b = by_slug["-home-operator-acme-net-tools"]
    assert b.primary_language == "python"


# ---------------------------------------------------------------------------
# 2. Machine regex
# ---------------------------------------------------------------------------


def test_machine_regex_picks_up_host_alpha_relay_and_metadata() -> None:
    text = (
        "ssh into host-alpha (192.168.50.42) — Tailscale 100.64.0.10 "
        "and we just got the RTX 3090 24GiB up. "
        "Separately, relay-fra-1 in paris is online. "
        "git.example.com is the Proxmox host."
    )
    machines: dict[str, MachineRecord] = {}
    _extract_machines_from_text(text, ts=1_700_000_000, machines=machines)

    assert "host-alpha" in machines
    wb = machines["host-alpha"]
    assert wb.lan_ip == "192.168.50.42"
    assert wb.tailscale_ip == "100.64.0.10"
    assert "RTX 3090" in wb.gpu
    assert "24GiB" in wb.gpu or "24GB" in wb.gpu

    assert "relay-fra-1" in machines
    # Role contains the region keyword — either "fra" (abbreviated) or "paris".
    role = machines["relay-fra-1"].role.lower()
    assert "relay" in role or "fra" in role or "paris" in role

    assert "git.example.com" in machines
    assert machines["git.example.com"].role == "Proxmox host"


# ---------------------------------------------------------------------------
# 3. Vocabulary lookup
# ---------------------------------------------------------------------------


def test_vocabulary_lookup_case_insensitive(memdb: sqlite3.Connection) -> None:
    # Seed one term and verify lookup with mixed casing.
    upsert_vocabulary_term(
        memdb,
        term="relay fleet",
        definition="5-VPS WireGuard fleet that fans out operator traffic.",
        category="service",
    )
    hit = get_term(memdb, "Relay Fleet")
    assert hit is not None
    assert "WireGuard" in hit["definition"]
    assert hit["category"] == "service"

    # Unknown term returns None.
    assert get_term(memdb, "definitely-not-a-real-term") is None


# ---------------------------------------------------------------------------
# 4. Frequency increment
# ---------------------------------------------------------------------------


def test_vocabulary_frequency_increments(memdb: sqlite3.Connection) -> None:
    upsert_vocabulary_term(
        memdb, term="KISS", definition="Keep it simple.", category="concept", frequency=3
    )
    before = get_term(memdb, "KISS")
    assert before is not None
    assert before["frequency"] == 3

    bump_vocabulary_frequency(memdb, "KISS", by=2)
    after = get_term(memdb, "KISS")
    assert after is not None
    assert after["frequency"] == 5

    # Bumping an unknown term is a no-op (no row created).
    bump_vocabulary_frequency(memdb, "unknown-term", by=99)
    assert get_term(memdb, "unknown-term") is None


# ---------------------------------------------------------------------------
# 5. Project upsert preserves prior fields
# ---------------------------------------------------------------------------


def test_project_upsert_coalesces_nulls(memdb: sqlite3.Connection) -> None:
    now = int(time.time())
    upsert_project(
        memdb,
        cwd="-home-operator-nova-api",
        display_name="nova-api",
        primary_language="go",
        message_count=123,
        first_seen_ts=now - 1000,
        last_active_ts=now - 500,
    )
    # Second pass only learns the purpose. The rest must survive.
    upsert_project(
        memdb,
        cwd="-home-operator-nova-api",
        purpose="bootstrap a new relay node",
    )
    row = get_project(memdb, "-home-operator-nova-api")
    assert row is not None
    assert row["purpose"] == "bootstrap a new relay node"
    assert row["primary_language"] == "go"
    assert row["message_count"] == 123
    assert row["display_name"] == "nova-api"


# ---------------------------------------------------------------------------
# 6. End-to-end: extract -> persist -> read back
# ---------------------------------------------------------------------------


def test_extract_and_persist_round_trip(
    synthetic_projects_root: Path, memdb: sqlite3.Connection
) -> None:
    snap = extract_ontology(synthetic_projects_root)
    written = persist_ontology(memdb, snap)
    assert written["projects"] >= 2
    # vocabulary includes at least the universal Claude-Code terms
    assert written["vocabulary"] >= len(_UNIVERSAL_CLAUDE_CODE_TERMS)
    # Machines: at least host-alpha should have landed from project A.
    assert written["machines"] >= 1

    projects = list_projects(memdb)
    slugs = {p["cwd"] for p in projects}
    assert "-home-operator-acme-net" in slugs

    machines = list_machines(memdb)
    hostnames = {m["hostname"] for m in machines}
    assert "host-alpha" in hostnames

    # Universal vocabulary terms should always be present.
    for v in _UNIVERSAL_CLAUDE_CODE_TERMS:
        term_row = get_term(memdb, v.term)
        assert term_row is not None, f"universal term {v.term!r} missing"
