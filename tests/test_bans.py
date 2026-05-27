"""Tests for the bans extractor + index + MCP tools.

Covers every pattern in the extractor spec, the public-vs-absolute
distinction, reassertion-count semantics, and failed-attempt capture +
lookup. The 8 tests below are the contract for this slice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from extractors.bans import Bans
from index.bans import (
    check_banned,
    ensure_schema,
    list_failed_attempts,
    upsert_ban,
    upsert_failed_attempt,
)


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


# ---------------------------------------------------------------------------
# 1. Each extractor pattern fires
# ---------------------------------------------------------------------------


def test_ban_pattern_absolute_never_recommend():
    rec = _user("never recommend provider-x for any deploy")
    out = [e for e in Bans().extract([rec]) if e.kind == "ban"]
    assert out, "absolute 'never recommend X' must fire"
    e = out[0]
    assert e.context["banned_thing"] == "provider-x"
    assert e.context["ban_strength"] == "absolute"
    assert "provider-x" in e.context["ban_text"].lower()
    assert "context_clause" not in e.context


def test_ban_pattern_preference_we_dont_use():
    rec = _user("we don't use stripe in this codebase")
    out = [e for e in Bans().extract([rec]) if e.kind == "ban"]
    assert out, "preference 'we don't use X' must fire"
    e = out[0]
    assert e.context["banned_thing"] == "stripe"
    assert e.context["ban_strength"] == "preference"


def test_ban_pattern_stop_suggesting_context():
    rec = _user("stop suggesting kubernetes, please")
    out = [e for e in Bans().extract([rec]) if e.kind == "ban"]
    assert out, "context 'stop suggesting X' must fire"
    e = out[0]
    assert e.context["banned_thing"] == "kubernetes"
    assert e.context["ban_strength"] == "context"


def test_ban_pattern_no_x_ever_absolute():
    rec = _user("no docker ever")
    out = [e for e in Bans().extract([rec]) if e.kind == "ban"]
    assert out, "absolute 'no X ever' must fire"
    e = out[0]
    assert e.context["banned_thing"] == "docker"
    assert e.context["ban_strength"] == "absolute"


# ---------------------------------------------------------------------------
# 2. Public vs absolute distinction
# ---------------------------------------------------------------------------


def test_public_vs_absolute_distinction(conn):
    """`never name provider-y publicly` is context-scoped with clause='publicly';
    `never use provider-x` is fully absolute (no context_clause).
    """
    rec_public = _user("never name provider-y publicly in the docs")
    rec_absolute = _user("never use provider-x")

    public_out = [e for e in Bans().extract([rec_public]) if e.kind == "ban"]
    abs_out = [e for e in Bans().extract([rec_absolute]) if e.kind == "ban"]

    # Public: at least one extraction with context clause = publicly.
    public_hits = [e for e in public_out if e.context.get("context_clause")]
    assert public_hits, "public ban must carry context_clause"
    assert public_hits[0].context["banned_thing"] == "provider-y"
    assert public_hits[0].context["context_clause"] == "publicly"
    assert public_hits[0].context["ban_strength"] == "context"

    # Absolute: no context_clause on the provider-x row.
    abs_hits = [e for e in abs_out if e.context["banned_thing"] == "provider-x"]
    assert abs_hits
    assert "context_clause" not in abs_hits[0].context
    assert abs_hits[0].context["ban_strength"] == "absolute"

    # Round-trip through the index: both can coexist, distinguished by clause.
    upsert_ban(
        conn,
        banned_thing="provider-y",
        ban_strength="context",
        ban_text=public_hits[0].context["ban_text"],
        context_clause="publicly",
    )
    upsert_ban(
        conn,
        banned_thing="provider-x",
        ban_strength="absolute",
        ban_text=abs_hits[0].context["ban_text"],
    )
    py = check_banned(conn, "provider-y")
    px = check_banned(conn, "provider-x")
    assert py and py["ban_strength"] == "context" and py["context_clause"] == "publicly"
    assert px and px["ban_strength"] == "absolute" and px["context_clause"] is None


# ---------------------------------------------------------------------------
# 3. Reassertion increments count
# ---------------------------------------------------------------------------


def test_reassertion_increments_count(conn):
    """Same (thing, strength, clause) tuple inserted twice → count = 2."""
    upsert_ban(
        conn,
        banned_thing="provider-x",
        ban_strength="absolute",
        ban_text="never use provider-x",
        ts=1_700_000_000,
    )
    upsert_ban(
        conn,
        banned_thing="provider-x",
        ban_strength="absolute",
        ban_text="seriously, never recommend provider-x",
        ts=1_700_001_000,
    )
    row = check_banned(conn, "provider-x")
    assert row is not None
    assert row["reassertion_count"] == 2
    # Newest quote wins; first_banned_ts pinned to the first write.
    assert "seriously" in row["ban_text"]
    assert row["first_banned_ts"] == 1_700_000_000
    assert row["last_reasserted_ts"] == 1_700_001_000


# ---------------------------------------------------------------------------
# 4. Failed-attempts capture (extractor + lookup)
# ---------------------------------------------------------------------------


def test_failed_attempts_capture_and_lookup(conn):
    """End-to-end: text → extractor → upsert → list_failed_attempts."""
    rec_switch = _assistant(
        "We switched from python-provider-y to provider-y-cloud because of auth and rate-limit issues."
    )
    rec_migrate = _user("MIGRATED AWAY FROM kubernetes after the cert rotation broke")
    rec_tried = _user("we tried fly.io but it failed on the persistent volume side")

    all_extractions = list(Bans().extract([rec_switch, rec_migrate, rec_tried]))
    failed = [e for e in all_extractions if e.kind == "failed_attempt"]

    # The three records should yield at least three distinct attempts.
    attempts = {e.context["attempt"].lower() for e in failed}
    assert "python-provider-y" in attempts
    assert "kubernetes" in attempts
    assert "fly.io" in attempts

    # The switched-from row must carry a replaced_by + reason.
    swap = [e for e in failed if e.context["attempt"].lower() == "python-provider-y"]
    assert swap
    assert swap[0].context.get("replaced_by", "").lower() == "provider-y-cloud"
    assert "auth" in (swap[0].context.get("reason") or "").lower()

    # Round-trip through the index.
    for e in failed:
        upsert_failed_attempt(
            conn,
            attempt=e.context["attempt"],
            replaced_by=e.context.get("replaced_by"),
            reason=e.context.get("reason"),
            cwd=e.cwd,
            attempted_ts=int(e.ts.timestamp()),
            abandoned_ts=int(e.ts.timestamp()),
        )

    # Lookup with topic filter.
    rows = list_failed_attempts(conn, topic="python-provider-y", limit=5)
    assert rows and rows[0]["attempt"] == "python-provider-y"
    assert rows[0]["replaced_by"] == "provider-y-cloud"

    # Lookup with no filter returns everything, newest first.
    all_rows = list_failed_attempts(conn, limit=10)
    assert len(all_rows) >= 3


# ---------------------------------------------------------------------------
# 8. MCP tool round-trip — confirms the user-facing surface returns the
#    documented shape end-to-end (extractor → index → MCP wrapper).
# ---------------------------------------------------------------------------


def test_mcp_check_banned_round_trip(conn, monkeypatch):
    """`check_banned(...)` MCP tool returns the documented dict shape."""
    # Seed via the extractor → indexer pipeline.
    rec = _user("never use provider-x — they nuked our box once already")
    bans_out = [e for e in Bans().extract([rec]) if e.kind == "ban"]
    assert bans_out
    for e in bans_out:
        upsert_ban(
            conn,
            banned_thing=e.context["banned_thing"],
            ban_strength=e.context["ban_strength"],
            ban_text=e.context["ban_text"],
            context_clause=e.context.get("context_clause"),
        )

    # Stub the MCP server's get_conn so the tool sees our in-memory DB.
    # The tool closes its conn after each call, so wrap it in a non-closing
    # proxy: every MCP call gets a fresh object whose .close() is a no-op
    # but whose .execute() defers to our backing in-memory connection.
    from mcp_server.extras import bans_tools

    class _NoCloseProxy:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, *a, **kw):
            return self._real.execute(*a, **kw)

        def executescript(self, *a, **kw):
            return self._real.executescript(*a, **kw)

        def close(self) -> None:  # tool calls this in `finally`
            pass

    monkeypatch.setattr(bans_tools, "get_conn", lambda: _NoCloseProxy(conn))

    hit = bans_tools.check_banned("provider-x")
    miss = bans_tools.check_banned("postgres")

    assert hit["banned"] is True
    assert hit["thing"] == "provider-x"
    assert hit["strength"] == "absolute"
    assert "provider-x" in hit["verbatim_quote"].lower()
    assert hit["context"] is None
    assert hit["reassertion_count"] >= 1

    assert miss["banned"] is False
    assert miss.get("thing") == "postgres"
