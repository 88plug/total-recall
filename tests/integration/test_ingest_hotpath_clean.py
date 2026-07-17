"""Guard test: ingest hot-path must not emit silenced warnings.

Bug class A: incremental profile updaters in ``_commit_parsed`` are wrapped in
``try/except -> log.warning(...)`` — so a real crash (e.g. workflow dict-vs-object
type mismatch) was silently swallowed and never failed a test.  This test catches
that class by:

1. Building a fully-synthetic JSONL corpus (no real operator PII, Dana-style).
2. Running the REAL ``ingest_all`` hot-path over it into a temp DB.
3. Asserting that NO log record at WARNING+ contains any of the forbidden
   "incremental update failed" substrings.
4. Asserting that the profile tables actually populated — proving the incrementals
   ran and didn't just silently no-op.

This test is hermetic (synthetic corpus, no ollama, no network) and MUST NOT skip.
It lives in tests/integration/ only to signal "end-to-end wiring", but it brings its
own corpus so it has no dependency on ~/.claude/projects.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Forbidden warning substrings — any match = a silenced crash slipped through.
# ---------------------------------------------------------------------------

FORBIDDEN_WARNING_SUBSTRINGS = [
    "operator_profile incremental update failed",
    "voice_profile incremental update failed",
    "vocabulary incremental update failed",
    "workflow incremental update failed",
    "implicit_preferences incremental update failed",
    "satisfaction incremental update failed",
    "incremental profile updates failed (non-fatal)",
]


# ---------------------------------------------------------------------------
# Synthetic corpus helpers (Dana-style, zero real PII)
# ---------------------------------------------------------------------------

_SESSION_A = str(uuid.UUID(int=0xAAAA_0001))
_SESSION_B = str(uuid.UUID(int=0xAAAA_0002))
_CWD = "/home/dana/synth-project"
_NOW = datetime(2026, 5, 28, 14, 0, 0, tzinfo=timezone.utc).isoformat()


def _user_record(text: str, session_id: str, uid: str, parent: str | None = None) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": session_id,
        "timestamp": _NOW,
        "cwd": _CWD,
        "gitBranch": "main",
        "message": {
            "role": "user",
            "content": text,
        },
    }


def _assistant_record(text: str, session_id: str, uid: str, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": session_id,
        "timestamp": _NOW,
        "cwd": _CWD,
        "gitBranch": "main",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "stop_reason": "end_turn",
        },
    }


def _ai_title_record(title: str, session_id: str) -> dict:
    return {
        "type": "ai-title",
        "sessionId": session_id,
        "timestamp": _NOW,
        "cwd": _CWD,
        "title": title,
    }


def _build_corpus(root: Path) -> Path:
    """Write a two-session synthetic corpus under a projects-root layout.

    Session A: Dana types a couple of user turns + assistant replies.
    Session B: A single turn, uses the KISS philosophy phrase (operator detector bait).
    Returns the projects_root path.
    """
    slug = _CWD.replace("/", "-")
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)

    def _write_session(session_id: str, records: list[dict]) -> None:
        path = slug_dir / f"{session_id}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Session A — multi-turn conversation
    records_a = [
        _ai_title_record("Dana's synth session A", _SESSION_A),
        _user_record("hi, i'm dana@synthcorp.example, starting fresh", _SESSION_A, "u-a1"),
        _assistant_record("Hello Dana! Let's get started.", _SESSION_A, "a-a1", "u-a1"),
        _user_record(
            "can you help me refactor the pipeline on novabox", _SESSION_A, "u-a2", "u-a1"
        ),
        _assistant_record("Sure! What does the pipeline do?", _SESSION_A, "a-a2", "u-a2"),
        _user_record("keep it simple please — KISS always wins here", _SESSION_A, "u-a3", "u-a2"),
        _assistant_record("Understood. KISS principles applied.", _SESSION_A, "a-a3", "u-a3"),
        _user_record(
            "also stop adding comments unless truly necessary", _SESSION_A, "u-a4", "u-a3"
        ),
        _assistant_record("Got it. No superfluous comments.", _SESSION_A, "a-a4", "u-a4"),
    ]
    _write_session(_SESSION_A, records_a)

    # Session B — shorter; operator email reassertion
    records_b = [
        _ai_title_record("Dana's synth session B", _SESSION_B),
        _user_record("dana@synthcorp.example checking in again", _SESSION_B, "u-b1"),
        _assistant_record("Welcome back!", _SESSION_B, "a-b1", "u-b1"),
        _user_record(
            "remind me: bias to action, verify before asserting", _SESSION_B, "u-b2", "u-b1"
        ),
        _assistant_record(
            "Absolutely, those are your core principles.", _SESSION_B, "a-b2", "u-b2"
        ),
    ]
    _write_session(_SESSION_B, records_b)

    return root


# ---------------------------------------------------------------------------
# The guard test
# ---------------------------------------------------------------------------


def test_ingest_hotpath_emits_no_incremental_warnings(tmp_path: pytest.TempPathFactory) -> None:
    """Ingest a synthetic corpus and assert zero incremental-update warnings.

    This is the test that would have caught the workflow dict-vs-object bug:
    ``workflow incremental update failed`` would have appeared in the log.
    """
    from index.db import connect
    from index.ingest import ingest_all
    from index.operator import get_profile
    from index.satisfaction import get_satisfaction_summary
    from index.voice import get_voice
    from index.workflow import get_workflow

    corpus_root = tmp_path / "projects"
    _build_corpus(corpus_root)

    db_path = tmp_path / "index.db"
    conn = connect(db_path)

    # Attach a log handler that collects every WARNING+ record from the
    # ingest module's logger hierarchy.
    captured_warnings: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                captured_warnings.append(record)

    handler = _Collector()
    handler.setLevel(logging.WARNING)

    # Capture from the root logger so we catch any sub-logger under index.*
    # or extractors.* that might re-route through a different name.
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    ingest_logger = logging.getLogger("index.ingest")
    ingest_logger.addHandler(handler)

    try:
        reports = ingest_all(
            conn=conn,
            projects_root=corpus_root,
            force_full=True,
            trigger="test",
            jobs=1,
        )
    finally:
        root_logger.removeHandler(handler)
        ingest_logger.removeHandler(handler)
        conn.close()

    # --- Assert 1: no forbidden warning substrings ---
    warning_messages = [r.getMessage() for r in captured_warnings]
    violations: list[str] = []
    for msg in warning_messages:
        for forbidden in FORBIDDEN_WARNING_SUBSTRINGS:
            if forbidden in msg:
                violations.append(f"  FORBIDDEN: {msg!r}")
                break  # one violation per message is enough to list

    assert not violations, (
        "Ingest hot-path emitted silenced incremental-update warnings — "
        "a real crash was swallowed:\n" + "\n".join(violations)
    )

    # --- Assert 2: at least two files ingested ---
    assert len(reports) >= 2, f"Expected >= 2 ingest reports, got {reports!r}"
    total_messages = sum(r.new_messages for r in reports)
    assert total_messages > 0, "No messages indexed — ingest did nothing"

    # Reopen read-write to query profile tables
    conn2 = connect(db_path)
    try:
        # --- Assert 3: operator_profile populated ---
        prof = get_profile(conn2)
        assert prof, "operator_profile is empty — incremental updater did not run"
        # The corpus mentions dana@synthcorp.example twice → should be detected
        email = prof.get("email_primary")
        assert email, (
            f"operator_profile.email_primary not set — operator NER incremental "
            f"did not fire.  Full profile: {prof}"
        )

        # --- Assert 4: voice_profile populated ---
        voice = get_voice(conn2)
        assert voice, "voice_profile is empty — incremental updater did not run"
        assert "lowercase_start_pct" in voice, (
            f"voice_profile missing expected key 'lowercase_start_pct'. "
            f"Got keys: {list(voice.keys())}"
        )
        sample_size = voice.get("sample_size", 0)
        assert sample_size > 0, (
            f"voice_profile.sample_size is 0 — no records were scored. Full voice profile: {voice}"
        )

        # --- Assert 5: workflow_profile populated ---
        wf = get_workflow(conn2)
        # Pop the timestamp sidecar before checking content.
        wf_data = {k: v for k, v in wf.items() if k != "_updated_ts"}
        assert wf_data, (
            "workflow_profile is empty — WorkflowProfile incremental did not run. "
            "This is exactly the bug class we guard against."
        )
        # sample_size field is set by extract_workflow_incremental
        wf_sample = wf_data.get("sample_size", 0)
        assert wf_sample > 0, (
            f"workflow_profile.sample_size is 0 — workflow extractor processed "
            f"nothing. Full workflow: {wf_data}"
        )

        # --- Assert 6: satisfaction tables exist (may be empty on tiny corpus) ---
        sat = get_satisfaction_summary(conn2)
        assert isinstance(sat, dict), f"get_satisfaction_summary returned non-dict: {sat!r}"
        # sample_size >= 0 (may be 0 if synthetic corpus has no clear
        # praise/frustration signals — that's fine; the point is it didn't crash).
        assert sat.get("sample_size", 0) >= 0

    finally:
        conn2.close()
