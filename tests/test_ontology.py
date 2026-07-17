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
    _UNIVERSAL_CLAUDE_CODE_TERMS,
    MachineRecord,
    _extract_machines_from_text,
    _looks_like_hex_id,
    extract_ontology,
    extract_ontology_from_records,
    mine_vocabulary,
    persist_ontology,
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
                    "content": (
                        "wire up acme-net-ctrl on host-alpha (192.168.50.42) "
                        "and fan out via the relay fleet"
                    ),
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
                            "text": (
                                "Editing main.go and api.go on host-alpha. "
                                "The RTX 3090 24GiB is up. Tailscale IP 100.64.0.10."
                            ),
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
                    "content": [{"type": "text", "text": "Patching parse.py — KISS applies."}],
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
    # related_projects is now a list (co-mention graph); may be empty if the
    # synthetic session text doesn't literally mention the sibling project name.
    assert isinstance(a.related_projects, list)

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


# ---------------------------------------------------------------------------
# 7. Co-mention graph populates related_projects
# ---------------------------------------------------------------------------


def test_co_mention_graph_populates_related_projects(tmp_path: Path) -> None:
    """Synthetic 3-project corpus: A mentions B 10x and C 2x.

    Verifies:
    - related_projects is ordered by mention_count descending.
    - self-references are skipped.
    - projects with no cross-mentions get an empty list (not None).
    - the result has the expected structure {cwd, display_name, mention_count}.
    """
    root = tmp_path / "projects"
    root.mkdir()

    # Slugs are chosen so that each has a unique non-generic basename that the
    # co-mention extractor can match.  We use multi-segment slugs to exercise
    # the candidate-name derivation path.
    slug_a = "-home-op-falcon-core"
    slug_b = "-home-op-raptor-engine"
    slug_c = "-home-op-viper-tools"

    # Names the extractor will derive from the slugs:
    #   slug_a → "falcon-core" (or just "falcon"/"core")
    #   slug_b → "raptor-engine" (or "raptor")
    #   slug_c → "viper-tools" (or "viper")
    # We embed these literal names in the session text so matching is reliable.

    def _sess(proj_dir: Path, stem: str, records: list[dict]) -> None:
        path = proj_dir / f"{stem}.jsonl"
        with path.open("w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    # Project A — mentions "raptor-engine" 10 times and "viper-tools" 2 times
    # in its single session.  Self-mentions of "falcon-core" must be skipped.
    a_dir = root / slug_a
    a_dir.mkdir()
    raptor_mentions = " raptor-engine" * 10
    viper_mentions = " viper-tools" * 2
    _sess(
        a_dir,
        "0" * 32,
        [
            {
                "type": "user",
                "sessionId": "sA",
                "uuid": "uA1",
                "timestamp": "2026-05-01T10:00:00Z",
                "cwd": "/home/op/falcon-core",
                "message": {
                    "role": "user",
                    "content": ("Working on falcon-core." + raptor_mentions + viper_mentions),
                },
            }
        ],
    )

    # Project B — no cross-mentions; related_projects should be empty.
    b_dir = root / slug_b
    b_dir.mkdir()
    _sess(
        b_dir,
        "1" * 32,
        [
            {
                "type": "user",
                "sessionId": "sB",
                "uuid": "uB1",
                "timestamp": "2026-05-01T10:00:00Z",
                "cwd": "/home/op/raptor-engine",
                "message": {
                    "role": "user",
                    "content": "standalone raptor-engine work, no cross-project refs",
                },
            }
        ],
    )

    # Project C — no cross-mentions.
    c_dir = root / slug_c
    c_dir.mkdir()
    _sess(
        c_dir,
        "2" * 32,
        [
            {
                "type": "user",
                "sessionId": "sC",
                "uuid": "uC1",
                "timestamp": "2026-05-01T10:00:00Z",
                "cwd": "/home/op/viper-tools",
                "message": {
                    "role": "user",
                    "content": "standalone viper-tools session",
                },
            }
        ],
    )

    snap = extract_ontology(root)
    by_slug = {p.cwd: p for p in snap.projects}

    assert slug_a in by_slug, "project A not discovered"
    assert slug_b in by_slug, "project B not discovered"
    assert slug_c in by_slug, "project C not discovered"

    a = by_slug[slug_a]

    # related_projects must be a list (never None)
    assert isinstance(a.related_projects, list), "related_projects must be a list"

    # Should have exactly 2 entries (B and C)
    assert len(a.related_projects) == 2, (
        f"expected 2 related projects for A, got {len(a.related_projects)}: {a.related_projects}"
    )

    # Each entry has the expected keys.
    for entry in a.related_projects:
        assert "cwd" in entry
        assert "display_name" in entry
        assert "mention_count" in entry

    # Ordered by mention_count descending — B (10) before C (2).
    first, second = a.related_projects
    assert first["cwd"] == slug_b, f"expected B first, got {first}"
    assert first["mention_count"] == 10, f"expected 10 mentions of B, got {first['mention_count']}"
    assert second["cwd"] == slug_c, f"expected C second, got {second}"
    assert second["mention_count"] == 2, f"expected 2 mentions of C, got {second['mention_count']}"

    # Self-reference: "falcon-core" must not appear in A's related_projects.
    related_cwds = {e["cwd"] for e in a.related_projects}
    assert slug_a not in related_cwds, "self-reference must be excluded"

    # B and C have no cross-mentions — their related_projects are empty lists.
    b = by_slug[slug_b]
    assert isinstance(b.related_projects, list)
    assert b.related_projects == [], f"B should have no related projects, got {b.related_projects}"

    c = by_slug[slug_c]
    assert isinstance(c.related_projects, list)
    assert c.related_projects == [], f"C should have no related projects, got {c.related_projects}"

    # Verify round-trip through persist_ontology writes the richer dict format.
    memdb = sqlite3.connect(":memory:", isolation_level=None)
    memdb.row_factory = sqlite3.Row
    ensure_schema(memdb)
    persist_ontology(memdb, snap)

    from index.ontology import get_project

    row_a = get_project(memdb, slug_a)
    assert row_a is not None
    rp = row_a["related_projects"]
    # _row_to_project decodes JSON into a list; our dict entries survive.
    assert isinstance(rp, list)
    assert len(rp) == 2
    # After persist + read-back the dicts are preserved.
    assert rp[0]["mention_count"] == 10
    assert rp[1]["mention_count"] == 2


# ---------------------------------------------------------------------------
# 8. Vocabulary specificity filter — generic English words are dropped
# ---------------------------------------------------------------------------


def _write_session_file(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _make_user_record(session_id: str, uuid: str, content: str, cwd: str) -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "uuid": uuid,
        "timestamp": "2026-05-01T10:00:00Z",
        "cwd": cwd,
        "message": {"role": "user", "content": content},
    }


def test_mine_vocabulary_drops_generic_english(tmp_path: Path) -> None:
    """Generic English words and the operator's username must NOT be promoted.

    Synthetic corpus: user turns in 3 sessions each repeat
    ``home``, ``name``, ``state``, ``read``, and ``andrew`` 20 times.
    None of these should appear in the promoted vocabulary.
    """
    root = tmp_path / "projects"
    root.mkdir()
    proj = root / "-home-andrew-myproject"
    proj.mkdir()

    # Three sessions, each with many occurrences of generic words.
    generic_words = ["home", "name", "state", "read", "andrew"]
    content = " ".join(w for w in generic_words for _ in range(20))

    for i in range(3):
        sess_path = proj / f"{'a' * 31}{i}.jsonl"
        _write_session_file(
            sess_path,
            [_make_user_record(f"s{i}", f"u{i}", content, "/home/andrew/myproject")],
        )

    terms = mine_vocabulary(root, min_sessions=2, min_freq=4)
    promoted_names = {t.term for t in terms}

    for word in generic_words:
        assert word not in promoted_names, (
            f"generic word {word!r} must not be promoted to vocabulary, "
            f"but it was. All promoted: {sorted(promoted_names)}"
        )


def test_mine_vocabulary_keeps_operator_specific(tmp_path: Path) -> None:
    """Operator-specific terms must be promoted; generic ones must not.

    Synthetic corpus:
    - ``opnsense``      15× across 3 sessions  (bare alpha, not English, len>=5)
    - ``wireguard``      8× across 3 sessions  (bare alpha, not English, len>=5)
    - ``relay-eu-west``  6× across 2 sessions  (hyphenated compound — always kept)
    - ``home``/``name``/``state``/``read``/``andrew`` 20× per session (generic)

    Expect: opnsense, wireguard, relay-eu-west promoted; generics dropped.
    Note: we avoid ``sidecar`` here because it exists in /usr/share/dict/words
    and would be filtered on systems that have the system wordlist installed.
    """
    root = tmp_path / "projects"
    root.mkdir()
    proj = root / "-home-andrew-infra"
    proj.mkdir()

    generic_blob = " ".join(
        w for w in ["home", "name", "state", "read", "andrew"] for _ in range(20)
    )

    # 3 sessions: all mention opnsense 5×, wireguard 3×, relay-eu-west 2× each.
    for i in range(3):
        specific_blob = (
            " opnsense" * 5
            + " wireguard" * 3
            + (" relay-eu-west" * 2 if i < 2 else "")  # relay-eu-west only in first 2
        )
        content = generic_blob + specific_blob
        sess_path = proj / f"{'b' * 31}{i}.jsonl"
        _write_session_file(
            sess_path,
            [_make_user_record(f"s{i}", f"u{i}", content, "/home/andrew/infra")],
        )

    terms = mine_vocabulary(root, min_sessions=2, min_freq=4)
    promoted_names = {t.term for t in terms}

    # Specific terms must be present.
    assert "opnsense" in promoted_names, (
        f"'opnsense' must be promoted but was not. Promoted: {sorted(promoted_names)}"
    )
    assert "wireguard" in promoted_names, (
        f"'wireguard' must be promoted but was not. Promoted: {sorted(promoted_names)}"
    )
    assert "relay-eu-west" in promoted_names, (
        f"'relay-eu-west' must be promoted but was not. Promoted: {sorted(promoted_names)}"
    )

    # Generic words must not be present.
    for word in ["home", "name", "state", "read", "andrew"]:
        assert word not in promoted_names, (
            f"generic word {word!r} must not be promoted. Promoted: {sorted(promoted_names)}"
        )


# ---------------------------------------------------------------------------
# 9. Vocabulary miner drops harness/tooling/path artifacts
# ---------------------------------------------------------------------------


def test_mine_vocabulary_drops_harness_artifacts(tmp_path: Path) -> None:
    """Session-mechanics noise must never reach the vocabulary table.

    Artifacts like task-notification blocks, tool-call IDs, tmp dir names,
    cwd slugs, and ls permission strings all contain hyphens so they pass the
    specificity gate — the _is_harness_artifact filter is the only thing
    that removes them.

    Synthetic corpus: 3 sessions, each user turn contains the harness tokens
    many times.  A real operator term (relay-eu-west) is also included to
    confirm the filter is not over-broad.
    """
    root = tmp_path / "projects"
    root.mkdir()
    proj = root / "-home-dana-myproj"  # fixture name uses generic 'dana', not real operator
    proj.mkdir()

    artifact_tokens = [
        "task-notification",  # harness XML block
        "tool-use-id",  # exact exact blocklist
        "output-file",  # exact blocklist
        "rw-rw-r",  # unix perms
        "toolu_abc123",  # tool-call ID (toolu_ prefix)
        "claude-1000",  # tmp dir (claude- + digits)
        "home-dana-myproj",  # cwd slug with leading home- segment
    ]
    # Legit operator term that must survive the filter.
    legit_token = "relay-eu-west"

    # Repeat each artifact 20× so they'd easily pass the freq/session thresholds
    # if the filter wasn't working.
    artifact_blob = " ".join(tok for tok in artifact_tokens for _ in range(20))
    content = artifact_blob + (" " + legit_token) * 10

    for i in range(3):
        sess_path = proj / f"{'c' * 31}{i}.jsonl"
        _write_session_file(
            sess_path,
            [_make_user_record(f"sH{i}", f"uH{i}", content, "/home/dana/myproj")],
        )

    terms = mine_vocabulary(root, min_sessions=2, min_freq=4)
    promoted_names = {t.term for t in terms}

    # All artifacts must be absent.
    for art in artifact_tokens:
        assert art not in promoted_names, (
            f"harness artifact {art!r} must not be promoted to vocabulary, "
            f"but it was. All promoted: {sorted(promoted_names)}"
        )

    # The real operator term must survive.
    assert legit_token in promoted_names, (
        f"legit operator term {legit_token!r} must be promoted, "
        f"but was not. All promoted: {sorted(promoted_names)}"
    )


# ---------------------------------------------------------------------------
# 10. extract_ontology_from_records — record-stream entry point
# ---------------------------------------------------------------------------


def _user_rec(session_id: str, content: str, ts: str = "2025-05-01T10:00:00.000Z") -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"role": "user", "content": content},
    }


def test_extract_ontology_from_records_machines() -> None:
    """Machine regex runs over record text via the record-stream path."""
    from extractors.ontology import OntologySnapshot

    text = (
        "Configure host-alpha (192.168.50.10) — Tailscale 100.64.0.5. "
        "relay-fra-1 is the paris node."
    )
    records = [_user_rec("sM1", text)]
    snap = extract_ontology_from_records(records)
    assert isinstance(snap, OntologySnapshot)
    assert snap.projects == [], "projects must be empty (directory walk deferred)"
    hostnames = {m.hostname for m in snap.machines}
    assert "host-alpha" in hostnames, f"host-alpha missing from {hostnames}"
    ha = next(m for m in snap.machines if m.hostname == "host-alpha")
    assert ha.lan_ip == "192.168.50.10"
    assert ha.tailscale_ip == "100.64.0.5"


def test_extract_ontology_from_records_vocabulary() -> None:
    """Vocabulary terms promoted across multiple sessions survive the filters."""
    # 3 sessions, each mentioning opnsense 5× and relay-eu-west 2×
    # → both should pass VOCAB_MIN_SESSIONS=2 and VOCAB_MIN_FREQ=4.
    records = []
    for i in range(3):
        content = " opnsense" * 5 + " relay-eu-west" * 2
        records.append(_user_rec(f"sV{i}", content))

    snap = extract_ontology_from_records(records)
    promoted = {v.term for v in snap.vocabulary}
    assert "opnsense" in promoted, f"opnsense not promoted; got {sorted(promoted)}"
    assert "relay-eu-west" in promoted, f"relay-eu-west not promoted; got {sorted(promoted)}"
    # Universal terms must always be present.
    for v in _UNIVERSAL_CLAUDE_CODE_TERMS:
        assert v.term in promoted, f"universal term {v.term!r} missing"


def test_extract_ontology_from_records_generic_words_dropped() -> None:
    """Generic English words must not be promoted even with high frequency."""
    records = []
    for i in range(3):
        content = " home" * 20 + " name" * 20 + " state" * 20
        records.append(_user_rec(f"sG{i}", content))

    snap = extract_ontology_from_records(records)
    promoted = {v.term for v in snap.vocabulary}
    for word in ["home", "name", "state"]:
        assert word not in promoted, f"generic word {word!r} must not be promoted"


def test_extract_ontology_from_records_record_objects() -> None:
    """Record objects with .raw dicts are handled the same as raw dicts."""

    class FakeRecord:
        def __init__(self, raw):
            self.raw = raw
            self.type = raw["type"]

    raw = _user_rec(
        "sRec1", "opnsense opnsense opnsense wireguard wireguard wireguard host-beta (10.0.0.5)"
    )
    # Three sessions, each with a FakeRecord carrying the same content.
    records = [FakeRecord({**raw, "sessionId": f"sRec{i}"}) for i in range(3)]
    snap = extract_ontology_from_records(records)
    # Machines should be extracted from the text.
    assert any(m.hostname == "host-beta" for m in snap.machines), (
        f"host-beta missing from {[m.hostname for m in snap.machines]}"
    )


def test_extract_ontology_from_records_empty() -> None:
    """Empty input returns a valid snapshot with universal vocab only."""
    snap = extract_ontology_from_records([])
    assert snap.projects == []
    assert snap.machines == []
    # Universal terms are always added.
    assert len(snap.vocabulary) == len(_UNIVERSAL_CLAUDE_CODE_TERMS)


@pytest.mark.parametrize(
    "h",
    [
        "a013-4f1a-9a75-13c768f26f92",
        "a01d-5333afd7bc86",
        "a025ca8d7a59c5e9-iad",
        "deadbeef-cafe-1234",
        "5333afd7bc86",
    ],
)
def test_hex_id_fragments_rejected_as_machines(h):
    assert _looks_like_hex_id(h) is True


@pytest.mark.parametrize(
    "h",
    [
        "relay-eu-west",
        "server-01",
        "db-prod",
        "wild-nuc",
        "host-01",
        "ap-southeast",
    ],
)
def test_real_hostnames_not_hex_rejected(h):
    assert _looks_like_hex_id(h) is False
