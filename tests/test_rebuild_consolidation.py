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

from extractors.operator_profile import extract_operator_profile
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

    noisy = "look at github.com/widgetlib — github.com/widgetlib is great, github.com/widgetlib again"
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
