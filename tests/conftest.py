"""Shared pytest fixtures for unit + integration tests.

Fixtures are defensive: if a sibling worktree's module is not yet merged,
tests that depend on it should `pytest.skip` rather than error at collection.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

# --- DB schema (minimal, mirrors index/schema.sql if present) ---------------

_DEFAULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd TEXT,
    ai_title TEXT,
    last_prompt TEXT,
    first_seen TEXT,
    last_seen TEXT,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    uuid TEXT PRIMARY KEY,
    session_id TEXT,
    parent_uuid TEXT,
    type TEXT,
    role TEXT,
    timestamp TEXT,
    cwd TEXT,
    is_sidechain INTEGER DEFAULT 0,
    content TEXT
);

CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    message_uuid TEXT,
    cwd TEXT,
    kind TEXT,
    topic TEXT,
    content TEXT,
    score REAL DEFAULT 0.0,
    timestamp TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_cwd ON messages(cwd);
CREATE INDEX IF NOT EXISTS idx_extractions_kind ON extractions(kind);
CREATE INDEX IF NOT EXISTS idx_extractions_cwd ON extractions(cwd);
CREATE INDEX IF NOT EXISTS idx_extractions_topic ON extractions(topic);
"""


def _load_schema() -> str:
    """Prefer real index/schema.sql when present; fall back to embedded."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    candidate = repo / "index" / "schema.sql"
    if candidate.exists():
        try:
            return candidate.read_text()
        except OSError:
            pass
    return _DEFAULT_SCHEMA


@pytest.fixture
def tmp_db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite seeded with the project schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_load_schema())
    try:
        yield conn
    finally:
        conn.close()


# --- Synthetic corpus -------------------------------------------------------

def _synth_records(session_id: str, cwd: str) -> list[dict]:
    base_ts = "2025-05-01T12:00:00.000Z"
    return [
        {
            "type": "ai-title",
            "sessionId": session_id,
            "uuid": f"{session_id}-title",
            "timestamp": base_ts,
            "cwd": cwd,
            "title": "tiny test session about provider-x and provider-y",
        },
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": f"{session_id}-u1",
            "parentUuid": None,
            "timestamp": base_ts,
            "cwd": cwd,
            "message": {"role": "user", "content": "deploy the relay on provider-x"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": f"{session_id}-a1",
            "parentUuid": f"{session_id}-u1",
            "timestamp": base_ts,
            "cwd": cwd,
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Using provider-x for the relay deploy."}
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": f"{session_id}-u2",
            "parentUuid": f"{session_id}-a1",
            "timestamp": base_ts,
            "cwd": cwd,
            "message": {"role": "user", "content": "no, use provider-y not provider-x"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": f"{session_id}-a2",
            "parentUuid": f"{session_id}-u2",
            "timestamp": base_ts,
            "cwd": cwd,
            "message": {
                "id": "msg_2",
                "model": "claude-opus-4-7",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Switching to provider-y as requested."}
                ],
            },
        },
        {
            "type": "last-prompt",
            "sessionId": session_id,
            "uuid": f"{session_id}-lp",
            "timestamp": base_ts,
            "cwd": cwd,
            "content": "no, use provider-y not provider-x",
            "leafUuid": f"{session_id}-u2",
        },
    ]


@pytest.fixture
def tiny_corpus(tmp_path: pathlib.Path) -> pathlib.Path:
    """A synthetic ~/.claude/projects-style corpus root with one tiny session."""
    cwd_slug = "-home-operator-fake-project"
    cwd_real = "/home/operator/fake-project"
    proj_dir = tmp_path / "projects" / cwd_slug
    proj_dir.mkdir(parents=True)
    session_id = "00000000-0000-0000-0000-000000000001"
    session_file = proj_dir / f"{session_id}.jsonl"
    with session_file.open("w") as fh:
        for rec in _synth_records(session_id, cwd_real):
            fh.write(json.dumps(rec) + "\n")
        # blank line should be tolerated by walker
        fh.write("\n")
    return tmp_path / "projects"


# --- Real corpus (read-only) ------------------------------------------------

_REAL_CORPUS = pathlib.Path("~/.claude/projects").expanduser()


@pytest.fixture(scope="session")
def real_corpus_root() -> pathlib.Path:
    if not _REAL_CORPUS.exists():
        pytest.skip(f"no real corpus at {_REAL_CORPUS}")
    return _REAL_CORPUS


@pytest.fixture(scope="session")
def real_corpus_smallest_session(real_corpus_root: pathlib.Path) -> pathlib.Path:
    """Locate the smallest non-empty .jsonl in the real corpus, read-only.

    Skips if corpus is absent or empty. Returned path must not be written to.
    """
    smallest: tuple[int, pathlib.Path] | None = None
    for jsonl in real_corpus_root.glob("*/*.jsonl"):
        try:
            sz = jsonl.stat().st_size
        except OSError:
            continue
        if sz <= 0:
            continue
        if smallest is None or sz < smallest[0]:
            smallest = (sz, jsonl)
    if smallest is None:
        pytest.skip("no non-empty .jsonl in real corpus")
    return smallest[1]
