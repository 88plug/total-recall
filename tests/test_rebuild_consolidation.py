"""Tests for `total-recall rebuild`'s consolidation pass (cold-path reconcile).

During ingest the operator profile is updated per-file via append-supersede,
which can freeze an early, non-global winner for frequency-ranked identity
scalars (e.g. a handle decided by one noisy file). `rebuild` runs a final
single-pass consolidation over the whole corpus so the persisted profile
matches the global full-pass extraction. These tests pin that guarantee.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from click.testing import CliRunner

from extractors.operator_profile import (
    extract_operator_profile,
    extract_operator_profile_from_records,
)
from index.db import connect
from index.operator import get_profile
from total_recall.__main__ import cli


def _user(text: str, cwd: str, sid: str) -> dict:
    return {
        "type": "user", "sessionId": sid, "cwd": cwd,
        "timestamp": "2026-05-01T10:00:00Z",
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str, cwd: str, sid: str) -> dict:
    return {
        "type": "assistant", "sessionId": sid, "cwd": cwd,
        "timestamp": "2026-05-01T10:00:01Z",
        "message": {
            "id": "m", "model": "claude-opus-4-7", "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _write_session(root: Path, slug: str, recs: list[dict]) -> None:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    sid = recs[0]["sessionId"]
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _build_corpus(tmp_path: Path) -> Path:
    """Multi-file corpus where per-file merge would freeze the wrong handle.

    File A: a noisy unrelated project handle `widgetlib` mentioned many times,
            with NO operator email present.
    File B: the operator's email `dana@novabox.io` + their own handle `dana`
            (thin) + a real timezone + a code fragment that must NOT be read
            as a timezone.
    Globally, email-corroboration boosts `dana` over the higher-frequency
    `widgetlib`; the consolidation pass must reflect that.
    """
    corpus = tmp_path / "projects"
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    cwd_a = "/home/dana/proj-a"
    cwd_b = "/home/dana/proj-b"

    noisy = (
        "look at github.com/widgetlib — github.com/widgetlib is great, "
        "github.com/widgetlib again"
    )
    recs_a = [_user(noisy, cwd_a, sid_a), _assistant(noisy, cwd_a, sid_a)] * 3

    recs_b = [
        _user(
            "im dana, email dana@novabox.io. see github.com/dana. "
            "timezone Europe/Berlin. note that Jc/Jmin/Jmax is a code path, not a zone.",
            cwd_b, sid_b,
        ),
        _assistant("confirmed dana@novabox.io, github.com/dana", cwd_b, sid_b),
    ]

    _write_session(corpus, "-home-dana-proj-a", recs_a)
    _write_session(corpus, "-home-dana-proj-b", recs_b)
    return corpus


def _run_rebuild(corpus: Path, db: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["--db", str(db), "rebuild", "--yes", "--projects-root", str(corpus)],
    )
    assert res.exit_code == 0, f"rebuild failed: {res.output}\n{res.exception!r}"


def test_rebuild_consolidates_handle_to_global_winner(tmp_path: Path) -> None:
    corpus = _build_corpus(tmp_path)
    db = tmp_path / "index.db"
    _run_rebuild(corpus, db)

    # The consolidation guarantee: persisted identity == global full-pass result.
    expected = extract_operator_profile(sorted(corpus.glob("*/*.jsonl")))
    persisted = get_profile(connect(db, read_only=True))

    assert persisted.get("handle") == expected.handle, (
        f"persisted handle {persisted.get('handle')!r} != full-pass {expected.handle!r} "
        "— consolidation did not reconcile to the global winner"
    )
    # The email-corroborated handle must win over the noisier unrelated project.
    assert persisted.get("handle") in ("dana", "novabox")
    assert persisted.get("handle") != "widgetlib"


def test_rebuild_timezone_never_garbage(tmp_path: Path) -> None:
    corpus = _build_corpus(tmp_path)
    db = tmp_path / "index.db"
    _run_rebuild(corpus, db)

    persisted = get_profile(connect(db, read_only=True))
    tz = persisted.get("timezone") or ""
    # Never the code-fragment garbage; must be empty or a real IANA zone/abbrev.
    assert "Jc" not in tz and "Jmin" not in tz and "Jmax" not in tz
    valid_prefixes = {
        "Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic",
        "Australia", "Europe", "Indian", "Pacific", "US", "Etc", "GMT",
    }
    if tz and "/" in tz:
        assert tz.split("/")[0] in valid_prefixes, f"non-IANA timezone persisted: {tz!r}"


def test_rebuild_profile_has_no_credential_fields(tmp_path: Path) -> None:
    corpus = _build_corpus(tmp_path)
    db = tmp_path / "index.db"
    _run_rebuild(corpus, db)
    persisted = get_profile(connect(db, read_only=True))
    for key in persisted:
        assert "password" not in key.lower()
        assert "secret" not in key.lower()
        assert key != "default_root_pw"


# ---------------------------------------------------------------------------
# Multi-source consolidation: from_records path
# ---------------------------------------------------------------------------


def _opencode_dict(text: str, cwd: str = "/home/operator/oc-proj") -> dict:
    """Minimal dict shaped like a normalized OpenCode record.

    ``source_file`` is set to a path outside ``~/.claude/`` so the extractor
    cannot confuse this with a claude_code JSONL record.
    """
    return {
        "type": "user",
        "sessionId": "oc-sess-rebuild",
        "cwd": cwd,
        "timestamp": "2026-05-01T15:00:00.000Z",
        "source_file": "/home/operator/.local/share/opencode/rebuild-session.json",
        "byte_offset": 0,
        "message": {"role": "user", "content": text},
    }


def _opencode_asst(text: str, cwd: str = "/home/operator/oc-proj") -> dict:
    return {
        "type": "assistant",
        "sessionId": "oc-sess-rebuild",
        "cwd": cwd,
        "timestamp": "2026-05-01T15:00:01.000Z",
        "source_file": "/home/operator/.local/share/opencode/rebuild-session.json",
        "byte_offset": 200,
        "message": {
            "id": "oc_msg_r",
            "model": "gpt-4o",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_from_records_multi_source_opencode_signal_surfaces() -> None:
    """``extract_operator_profile_from_records`` must surface identity signals
    carried by records tagged as opencode (non-claude_code ``source_file``).

    This exercises the same consolidation path that ``rebuild`` uses when it
    reads from ``lib.sources.collect`` (multi-source) rather than walking
    ``~/.claude/projects/*.jsonl`` directly.  By asserting on the from_records
    entry point we pin that the consolidation cannot silently regress to
    claude_code-only input.
    """
    oc_records = [
        _opencode_dict(
            "I'm Petra Nkosi (petranx). Email: petra@tessaract.dev. "
            "github.com/petranx is my profile. "
            "We run on Asia/Kolkata time. Never use heroku."
        ),
        _opencode_asst(
            "Got it — petra@tessaract.dev, github.com/petranx, Asia/Kolkata."
        ),
        _opencode_dict("Confirmed: petra@tessaract.dev is correct."),
        _opencode_asst("Your primary handle is github.com/petranx."),
    ]

    profile = extract_operator_profile_from_records(oc_records)

    assert profile.name == "Petra Nkosi", (
        f"Expected 'Petra Nkosi' from opencode records, got {profile.name!r} — "
        "multi-source consolidation dropped the opencode signal"
    )
    assert profile.email_primary == "petra@tessaract.dev", (
        f"Expected 'petra@tessaract.dev', got {profile.email_primary!r}"
    )
    assert profile.timezone == "Asia/Kolkata", (
        f"Expected 'Asia/Kolkata', got {profile.timezone!r}"
    )
    assert "heroku" in profile.banned_providers, (
        "banned_providers must include 'heroku' from opencode records"
    )
    handle = (profile.handle or profile.github_user or "").lower()
    assert handle in ("petranx", "tessaract"), (
        f"Expected handle 'petranx' (or domain label 'tessaract'), got {handle!r}"
    )


def test_from_records_mixed_sources_consolidates_to_global_winner() -> None:
    """When records come from both claude_code and opencode, the final profile
    must reflect the globally-strongest signal (email-corroborated handle wins
    over a noisier unrelated token), exactly as the rebuild consolidation
    guarantees for the file-based path.
    """
    noisy_cc = [
        _user(
            "look at github.com/noisylib — github.com/noisylib is great, "
            "github.com/noisylib again, github.com/noisylib repeated",
            cwd="/home/operator/cc-proj",
            sid="cc-sess-1",
        ),
        _assistant(
            "github.com/noisylib noisylib noisylib",
            cwd="/home/operator/cc-proj",
            sid="cc-sess-1",
        ),
    ] * 3

    signal_oc = [
        _opencode_dict(
            "im petra, email petra@tessaract.dev. see github.com/petranx. "
            "timezone Asia/Kolkata."
        ),
        _opencode_asst("confirmed petra@tessaract.dev, github.com/petranx"),
    ]

    profile = extract_operator_profile_from_records(noisy_cc + signal_oc)

    handle = (profile.handle or profile.github_user or "").lower()
    # Email-corroborated "petranx" must beat the higher-frequency noise token.
    assert handle != "noisylib", (
        f"Consolidation let noisylib win — email-corroboration logic broken for "
        f"multi-source input (got handle={handle!r})"
    )
    assert handle in ("petranx", "tessaract"), (
        f"Expected corroborated handle, got {handle!r}"
    )
