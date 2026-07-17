"""Validate that operator extractors work for any operator — not just the author.

This test builds a synthetic non-Andrew operator JSONL corpus (Dana Lopez /
danacodes / nova-box / Vultr / Stripe / Europe-Berlin) and asserts:

(a) The fake operator's values populate the extracted profile.
(b) ZERO Andrew literals appear in the extracted data.
(c) No password/secret field exists on the profile.

A full-pipeline variant (ingest → DB → query) is included but skipped when
the ingest layer is unavailable (keeps CI hermetic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from extractors.ontology import extract_ontology, persist_ontology
from extractors.operator_profile import (
    OperatorProfile,
    extract_operator_profile,
    persist_profile,
)
from extractors.voice_profile import measure_voice

# ---------------------------------------------------------------------------
# Foreign-operator literals — any appearance in the synthetic Dana operator's
# extracted data is a bug (proves the extractors don't bleed a hardcoded
# foreign identity into discovery). Committed sentinels are obviously
# fictional and publisher-safe. The original author's real private names live
# in a gitignored local denylist and are folded in only when present (so the
# guard is sharper on the operator's own machine without publishing names).
# ---------------------------------------------------------------------------

_FOREIGN_SENTINELS = {
    "globex",
    "initech",
    "umbrella-corp",
    "umbrella corp",
    "wayne enterprises",
    "stark industries",
    "acme widgets",
    "foobar@example.org",
    "10.255.255.254",
}


def _load_private_denylist() -> set[str]:
    import os

    path = Path(
        os.environ.get(
            "BT_AUTHOR_DENYLIST",
            str(Path(__file__).resolve().parents[1] / "tests" / "local" / "author_denylist.json"),
        )
    )
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(s).lower() for s in data.get("forbidden_literals", []) if str(s).strip()}


ANDREW_LITERALS = _FOREIGN_SENTINELS | _load_private_denylist()


def _contains_andrew_literal(obj: Any) -> bool:
    """Recursively check if any string value in `obj` contains a forbidden literal."""
    if isinstance(obj, str):
        lower = obj.lower()
        return any(lit in lower for lit in ANDREW_LITERALS)
    if isinstance(obj, dict):
        return any(_contains_andrew_literal(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_andrew_literal(item) for item in obj)
    return False


# ---------------------------------------------------------------------------
# Synthetic corpus builder
# ---------------------------------------------------------------------------


def _user(text: str) -> dict:
    return {
        "type": "user",
        "uuid": f"u-{abs(hash(text)) % 100_000}",
        "sessionId": "dana-sess-1",
        "cwd": "/home/dana/nova-api",
        "timestamp": "2026-05-01T12:00:00Z",
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{abs(hash(text)) % 100_000}",
        "sessionId": "dana-sess-1",
        "cwd": "/home/dana/nova-api",
        "timestamp": "2026-05-01T12:00:01Z",
        "message": {
            "id": "msg_dana_1",
            "model": "claude-opus-4-7",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _voice_record(text: str) -> dict:
    """User-string record that measure_voice() will consume."""
    return {"type": "user", "content_kind": "string", "text": text}


# Corpus records — generic operator "Dana Lopez" / danacodes / novacluster.io
DANA_CORPUS_RECORDS = [
    _user("Hi, this is Dana Lopez from NovaCluster."),
    _assistant("Got it! I'll use dana@novacluster.io for all git commits."),
    _user("My other email is ops@novacluster.io for ops alerts."),
    _assistant("Deploying to nova-box (10.10.0.5, tailscale 100.100.1.5) via ssh dana@nova-box."),
    _user(
        "Never recommend digitalocean for us. We use vultr exclusively. "
        "Billing goes through stripe."
    ),
    _assistant(
        "Confirmed — self-hosted gitlab on code.novacluster.io. "
        "github.com/danacodes is the public mirror."
    ),
    _user("Rules: KISS, single-dev maintainer, self-hosted everything. Timezone is Europe/Berlin."),
    _assistant("Products: nova-api, nova-agent, nova-ctl. ISP uplinks: Starlink and Ziply."),
    # Second confirm for email confidence bump.
    _assistant("Confirmed again at dana@novacluster.io."),
    # Voice-style short turns.
    _user("check the nova-api service"),
    _user("fix the nova-box deployment"),
    _user("run the test suite"),
    _user("we dont need the old endpoint"),
]


@pytest.fixture
def dana_corpus(tmp_path: Path) -> Path:
    """Write the Dana corpus to a temp JSONL file."""
    p = tmp_path / "dana-sess-1.jsonl"
    with p.open("w") as f:
        for rec in DANA_CORPUS_RECORDS:
            f.write(json.dumps(rec) + "\n")
    return p


@pytest.fixture
def dana_projects_root(tmp_path: Path) -> Path:
    """Build a minimal ~/.claude/projects/ tree for Dana."""
    root = tmp_path / "projects"
    root.mkdir()
    slug = "-home-dana-nova-api"
    proj = root / slug
    proj.mkdir()
    (proj / "dana-sess-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in DANA_CORPUS_RECORDS) + "\n"
    )
    return root


# ---------------------------------------------------------------------------
# (a) Operator profile extraction produces Dana's values
# ---------------------------------------------------------------------------


def test_extract_operator_profile_captures_dana_identity(dana_corpus: Path) -> None:
    profile = extract_operator_profile([dana_corpus])

    assert isinstance(profile, OperatorProfile)
    assert profile.name == "Dana Lopez"
    assert profile.email_primary == "dana@novacluster.io"
    assert "ops@novacluster.io" in profile.emails_alt
    assert "stripe" in profile.billing_rail
    # Banned: "digitalocean" is extracted by the never-recommend pattern.
    assert len(profile.banned_providers) >= 1
    assert "digitalocean" in profile.banned_providers
    assert profile.timezone == "Europe/Berlin"
    assert "Starlink" in profile.home_uplinks
    assert "KISS" in profile.philosophy
    # Machine: nova-box should appear via ssh context.
    assert len(profile.machines) >= 1


def test_no_password_field_on_profile(dana_corpus: Path) -> None:
    """The profile dataclass must not expose a password/secret field."""
    profile = extract_operator_profile([dana_corpus])
    assert not hasattr(profile, "default_root_pw")
    assert not hasattr(profile, "root_password")
    assert not hasattr(profile, "password")
    # to_field_dict() must not leak passwords.
    fdict = profile.to_field_dict()
    for key in fdict:
        assert "password" not in key.lower()
        assert "secret" not in key.lower()


# ---------------------------------------------------------------------------
# (b) Zero Andrew literals in extracted data
# ---------------------------------------------------------------------------


def test_no_andrew_literals_in_profile(dana_corpus: Path) -> None:
    """Dana's profile must contain zero Andrew-specific strings."""
    profile = extract_operator_profile([dana_corpus])
    fdict = profile.to_field_dict()

    for field_name, value in fdict.items():
        assert not _contains_andrew_literal(value), (
            f"Andrew literal found in profile field {field_name!r}: {value!r}"
        )

    # Also check confidence and sources dicts.
    assert not _contains_andrew_literal(profile.confidence)
    assert not _contains_andrew_literal(profile.sources)


def test_no_andrew_literals_in_ontology(dana_projects_root: Path) -> None:
    """Dana's ontology extraction must contain zero Andrew-specific strings."""
    snap = extract_ontology(dana_projects_root)

    for proj in snap.projects:
        assert not _contains_andrew_literal(proj.cwd), f"Andrew in project cwd: {proj.cwd}"
        assert not _contains_andrew_literal(proj.purpose)

    for machine in snap.machines:
        assert not _contains_andrew_literal(machine.hostname), (
            f"Andrew in machine hostname: {machine.hostname}"
        )

    for term in snap.vocabulary:
        # Universal terms are generic; operator terms come from the corpus.
        assert not _contains_andrew_literal(term.term)
        assert not _contains_andrew_literal(term.definition)


def test_no_andrew_literals_in_voice(dana_corpus: Path) -> None:
    """Voice profile for Dana must contain zero Andrew-specific strings."""
    voice_records = [
        {"type": "user", "content_kind": "string", "text": rec["message"]["content"]}
        for rec in DANA_CORPUS_RECORDS
        if rec.get("type") == "user" and isinstance(rec.get("message", {}).get("content"), str)
    ]
    voice = measure_voice(voice_records)
    assert not _contains_andrew_literal(voice), f"Andrew literal found in voice profile: {voice}"


# ---------------------------------------------------------------------------
# Full-pipeline variant: ingest → DB → query (hermetic, skips if unavailable)
# ---------------------------------------------------------------------------


def test_full_pipeline_no_andrew_leak(dana_projects_root: Path, tmp_path: Path) -> None:
    """Ingest Dana's corpus into a temp DB and verify no Andrew data leaks out."""
    pytest.importorskip("index.db", reason="index layer unavailable")

    from index.db import connect
    from index.ontology import ONTOLOGY_SCHEMA
    from index.operator import OPERATOR_PROFILE_SCHEMA, get_profile

    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    try:
        # Bootstrap the profile + ontology tables.
        conn.executescript(OPERATOR_PROFILE_SCHEMA)
        conn.executescript(ONTOLOGY_SCHEMA)

        # Run operator profile extractor and persist.
        profile = extract_operator_profile(
            [dana_projects_root / "-home-dana-nova-api" / "dana-sess-1.jsonl"]
        )
        persist_profile(conn, profile)

        # Run ontology extractor and persist.
        snap = extract_ontology(dana_projects_root)
        persist_ontology(conn, snap)

        # Query back and verify no Andrew literals.
        stored = get_profile(conn)
        assert not _contains_andrew_literal(
            {k: v for k, v in stored.items() if not k.startswith("_")}
        ), f"Andrew literal found in stored profile: {stored}"

        # Verify Dana's email survived the round-trip.
        assert stored.get("email_primary") == "dana@novacluster.io"

    finally:
        conn.close()
