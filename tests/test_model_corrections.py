"""Tests for the model_corrections extractor + its MCP tool surface.

The extractor is exercised via synthetic ``FakeRecord``s that implement the
:class:`extractors.base.RecordLike` protocol (same trick the rest of the
test suite uses — see ``tests/test_extractors.py``).

The MCP tool tests stub WT-4's ``index.query`` API with an in-memory SQLite
DB so we can verify the tool returns the documented shape without needing
the full pipeline to be wired up.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from extractors.model_corrections import ModelCorrections
from tests.mcp_helpers import call_tool

# ---------------------------------------------------------------------------
# Fixtures: minimal Record + DAG (mirrors test_extractors.py)
# ---------------------------------------------------------------------------


@dataclass
class FakeRecord:
    type: str
    uuid: str
    parent_uuid: str | None = None
    session_id: str = "sess-1"
    cwd: str = "/home/operator/proj"
    ts: datetime = field(
        default_factory=lambda: datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    )
    role: str | None = None
    content_kind: str | None = None
    content: Any = None
    text: str | None = None
    is_meta: bool = False
    is_compact_summary: bool = False
    is_sidechain: bool = False
    subtype: str | None = None
    payload: dict | None = None


def _user(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="user",
        uuid=kw.pop("uuid", f"u-{abs(hash(text)) % 10_000}"),
        role="user",
        content_kind="string",
        text=text,
        content=text,
        **kw,
    )


def _assistant(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="assistant",
        uuid=kw.pop("uuid", f"a-{abs(hash(text)) % 10_000}"),
        role="assistant",
        content_kind="blocks",
        text=text,
        content=[{"type": "text", "text": text}],
        **kw,
    )


class FakeDag:
    def __init__(self, records: list[FakeRecord]) -> None:
        self._records = records
        self._idx = {r.uuid: i for i, r in enumerate(records)}

    def get(self, uuid: str) -> FakeRecord | None:
        for r in self._records:
            if r.uuid == uuid:
                return r
        return None

    def parent_of(self, uuid: str) -> FakeRecord | None:
        rec = self.get(uuid)
        if rec is None or rec.parent_uuid is None:
            return None
        return self.get(rec.parent_uuid)

    def prev_assistant_turn(self, uuid: str) -> FakeRecord | None:
        i = self._idx.get(uuid)
        if i is None:
            return None
        for j in range(i - 1, -1, -1):
            if self._records[j].type == "assistant":
                return self._records[j]
        return None

    def next_user_turn(self, uuid: str, within: int = 5) -> FakeRecord | None:
        i = self._idx.get(uuid)
        if i is None:
            return None
        for j in range(i + 1, min(len(self._records), i + 1 + within)):
            if self._records[j].type == "user":
                return self._records[j]
        return None


# ---------------------------------------------------------------------------
# 1. Positive cases — one per spec pattern.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "no, that's wrong",  # directive flip
        "i told you we don't use provider-y",  # restatement (told)
        "i asked you to check first",  # restatement (asked)
        "you already tried that approach",  # already
        "how many times do I have to say it",  # how many times
        "stop using sed everywhere",  # stop using
        "stop guessing and check the docs",  # stop guessing
        "don't use Stripe for billing",  # don't use
        "never recommend provider-y again",  # never recommend
        "that's not what I asked for",  # that's not
        "wtf are you doing",  # wtf
        "we never use provider-y for relays",  # we never use
        "you're just guessing here",  # guessing
        "check our session logs first",  # cross-session memory appeal
        "you're drifting from the spec",  # drift callout
        "you broke the build again",  # severity-high
        "never ever push to main",  # strongest rule
    ],
)
def test_pattern_fires_on_positive_examples(text):
    rec = _user(text)
    hits = list(ModelCorrections().extract([rec]))
    assert hits, f"should match: {text!r}"
    assert hits[0].kind == "model_correction"


# ---------------------------------------------------------------------------
# 2. Severity scoring — profanity bonus stacks.
# ---------------------------------------------------------------------------


def test_profanity_and_terseness_drive_severity_high():
    # 30 chars, profanity, restatement => 0.5 + 0.2 + 0.1 + 0.1 = 0.9
    rec = _user("wtf, i already said no provider-y")
    hits = list(ModelCorrections().extract([rec]))
    assert len(hits) == 1
    ext = hits[0]
    assert ext.context["severity"] == pytest.approx(ext.score)
    # Severity floor for this combination of bonuses.
    assert ext.score >= 0.8


# ---------------------------------------------------------------------------
# 3. Escalation chain — second correction in a row gets the +0.1 bonus.
# ---------------------------------------------------------------------------


def test_escalation_chain_bumps_second_correction():
    # Two structurally-identical corrections back-to-back. Each matches one
    # pattern (don't use), is short (<50 chars => +0.1), no profanity, no
    # insult, no restatement-word. The ONLY differentiator should be the
    # escalation-chain bonus on the second turn.
    a = _assistant("Switching the relay to provider-y BHS.", uuid="a1")
    u1 = _user("don't use provider-y", uuid="u1", parent_uuid="a1")
    u2 = _user("don't use provider-y", uuid="u2", parent_uuid="u1")
    records = [a, u1, u2]
    dag = FakeDag(records)

    hits = list(ModelCorrections().extract(records, dag=dag))
    assert len(hits) == 2
    # Second hit should out-score the first by exactly the chain bonus (+0.1).
    assert hits[1].score > hits[0].score
    assert hits[1].score - hits[0].score == pytest.approx(0.1, abs=0.01)


# ---------------------------------------------------------------------------
# 4. Noise rejection — must NOT fire.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "yes please proceed",
        "looks good, ship it",
        "the no-op handler is fine",  # 'no' mid-sentence is not a directive
        "<task-notification>foo</task-notification>",
        "<command-name>/foo</command-name>",
        "<local-command-stdout>blah</local-command-stdout>",
        "ok",  # below MIN_LEN
    ],
)
def test_noise_does_not_match(text):
    rec = _user(text)
    assert not list(ModelCorrections().extract([rec])), f"should not match: {text!r}"


def test_meta_and_compact_records_are_skipped():
    rec = _user("no, stop doing that")
    rec.is_meta = True
    assert not list(ModelCorrections().extract([rec]))

    rec2 = _user("no, stop doing that")
    rec2.is_compact_summary = True
    assert not list(ModelCorrections().extract([rec2]))


def test_overlong_user_string_is_skipped():
    # MAX is 1500; pad past it.
    rec = _user("no " + ("x" * 1600))
    assert not list(ModelCorrections().extract([rec]))


# ---------------------------------------------------------------------------
# 5. Context capture — preceding assistant text + uuid populate the context.
# ---------------------------------------------------------------------------


def test_context_captures_preceding_assistant_text_and_uuid():
    long_action = ("Filler text. " * 50) + "Final action: switching relay to provider-y BHS."
    a_prev = _assistant(long_action, uuid="a-prev")
    u_corr = _user("no, we never use provider-y for relays", uuid="u-corr", parent_uuid="a-prev")
    records = [a_prev, u_corr]
    dag = FakeDag(records)

    hits = list(ModelCorrections().extract(records, dag=dag))
    assert len(hits) == 1
    ctx = hits[0].context

    assert ctx["preceding_uuid"] == "a-prev"
    # Last 400 chars only — original assistant text is much longer than that.
    assert ctx["rejected_approach"] is not None
    assert len(ctx["rejected_approach"]) <= 400
    # The truncation should preserve the *tail* (the action line).
    assert "provider-y" in ctx["rejected_approach"]
    assert ctx["correction"] == "no, we never use provider-y for relays"
    assert "severity" in ctx
    assert isinstance(ctx["severity"], float)


# ---------------------------------------------------------------------------
# 6. MCP tool surface — recall_corrections_about returns documented shape.
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            ts TEXT NOT NULL,
            source_uuid TEXT NOT NULL,
            score REAL NOT NULL,
            context TEXT
        );
        """
    )


@pytest.fixture
def tmp_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(tmp_path))
    for mod in (
        "mcp_server",
        "mcp_server.server",
        "mcp_server.tools",
        "mcp_server.resources",
        "mcp_server.extras",
        "mcp_server.extras.corrections_tools",
    ):
        sys.modules.pop(mod, None)
    return tmp_path


@pytest.fixture
def fake_index_query(monkeypatch: pytest.MonkeyPatch):
    fake = types.ModuleType("index.query")

    def search_extractions(conn, query=None, cwd=None, kind=None, limit=10, session_id=None):  # noqa: ANN001
        sql = "SELECT * FROM extractions WHERE 1=1"
        params: list = []
        if cwd:
            sql += " AND cwd = ?"
            params.append(cwd)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if query:
            sql += " AND content LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        return list(conn.execute(sql, params).fetchall())

    fake.search_extractions = search_extractions  # type: ignore[attr-defined]
    parent = types.ModuleType("index")
    parent.query = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index", parent)
    monkeypatch.setitem(sys.modules, "index.query", fake)
    return fake


def _seed_corrections(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    ts_old = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    ts_new = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    rows = [
        (
            1,
            "model_correction",
            "no, we never use provider-y for relays",
            "s1",
            "/home/operator/proj-a",
            ts_old,
            "u1",
            0.85,
            json.dumps(
                {
                    "rejected_approach": "Switching the relay to provider-y BHS.",
                    "correction": "no, we never use provider-y for relays",
                    "preceding_uuid": "a1",
                    "severity": 0.85,
                }
            ),
        ),
        (
            2,
            "model_correction",
            "stop suggesting provider-y already",
            "s1",
            "/home/operator/proj-a",
            ts_new,
            "u2",
            0.7,
            json.dumps(
                {
                    "rejected_approach": "provider-y is the cheapest option here.",
                    "correction": "stop suggesting provider-y already",
                    "preceding_uuid": "a2",
                    "severity": 0.7,
                }
            ),
        ),
        # Decoy: different topic.
        (
            3,
            "model_correction",
            "no, we don't use Stripe",
            "s2",
            "/home/operator/proj-b",
            ts_new,
            "u3",
            0.6,
            json.dumps(
                {
                    "rejected_approach": "Use Stripe Checkout.",
                    "correction": "no, we don't use Stripe",
                    "preceding_uuid": "a3",
                    "severity": 0.6,
                }
            ),
        ),
        # Decoy: wrong kind.
        (
            4,
            "decision",
            "Use provider-y for the relay fleet",
            "s3",
            "/home/operator/proj-a",
            ts_new,
            "u4",
            0.9,
            json.dumps({}),
        ),
    ]
    conn.executemany("INSERT INTO extractions VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _import_server_with_extras():
    """Fresh import of mcp_server.server then the extras tool module.

    Importing the tool module is what registers ``recall_corrections_about``
    and ``get_recent_corrections`` against the shared MCPServer instance.
    """
    server = importlib.import_module("mcp_server.server")
    importlib.import_module("mcp_server.extras.corrections_tools")
    return server


def test_recall_corrections_about_returns_documented_shape(tmp_db_dir, fake_index_query):
    _seed_corrections(tmp_db_dir / "index.db")
    server = _import_server_with_extras()

    _, structured = call_tool(
        server.mcp,
        "recall_corrections_about",
        {"topic": "provider-y", "scope": "all_projects", "limit": 10},
    )
    hits = structured["result"]
    assert isinstance(hits, list)
    # Two rows match "provider-y" (model_correction kind, content LIKE %provider-y%).
    # The decision row with provider-y must be filtered out by `kind`.
    assert len(hits) == 2
    for h in hits:
        # Documented shape:
        assert {
            "correction",
            "rejected_approach",
            "preceding_uuid",
            "severity",
            "session_id",
            "cwd",
            "ts",
        } <= h.keys()
        assert "provider-y" in (h["correction"] or "") or "provider-y" in (
            h["rejected_approach"] or ""
        )

    # Sort: severity DESC then ts DESC. Row 1 (sev 0.85) comes before row 2 (sev 0.70).
    assert hits[0]["severity"] >= hits[1]["severity"]
    assert hits[0]["preceding_uuid"] == "a1"
