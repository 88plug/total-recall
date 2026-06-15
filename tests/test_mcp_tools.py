"""Unit tests for the total-recall MCP server.

These tests exercise the FastMCP server in three modes:

1. **Tool registration shape** — every tool is registered with the expected
   name, has a non-empty description, has an input schema with the documented
   parameters, and (where applicable) the documented enum values.

2. **Missing-index degradation** — when the SQLite index file is absent the
   server returns a structured ``error`` payload rather than raising. This is
   the failure mode users will hit on first install before they run
   ``total-recall index``.

3. **End-to-end recall over a synthetic corpus** — we stub the WT-4
   ``index.query`` API with a minimal in-memory implementation, seed five
   mixed extractions, and verify that ``recall("provider-x", scope="all_projects")``
   returns them in score-descending order with the expected fields.

The tests avoid importing the optional vector layer (`fastembed`,
`sqlite-vec`) — they're not needed and the dense path is exercised separately
in the WT-5 test suite.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at a temp DB dir before it's imported."""
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(tmp_path))
    # Clear any cached server module so DB_PATH is re-resolved.
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    return tmp_path


@pytest.fixture
def server_module(tmp_db_dir: Path):
    """Import a fresh mcp_server with DB pointed at tmp dir."""
    import mcp_server  # noqa: F401

    return importlib.import_module("mcp_server.server")


@pytest.fixture
def fake_index_query(monkeypatch: pytest.MonkeyPatch):
    """Install a stub ``index.query`` module backed by an in-memory SQLite DB.

    The stub mirrors the public surface WT-4 documents:

    - ``search_extractions(conn, query, cwd=None, kind=None, limit=10, ...)``
    - ``search_messages(conn, query, cwd=None, role=None, limit=10)``
    - ``list_sessions_for_cwd(conn, cwd, limit=10)``

    Each returns ``list[sqlite3.Row]`` so the server-side conversion in
    :func:`mcp_server.tools._row_to_hit` is the path under test.
    """

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

    def search_messages(conn, query=None, cwd=None, role=None, limit=10):  # noqa: ANN001
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if cwd:
            sql += " AND cwd = ?"
            params.append(cwd)
        if role:
            sql += " AND role = ?"
            params.append(role)
        if query:
            sql += " AND text LIKE ?"
            params.append(f"%{query}%")
        sql += " LIMIT ?"
        params.append(limit)
        return list(conn.execute(sql, params).fetchall())

    def list_sessions_for_cwd(conn, cwd, limit=10):  # noqa: ANN001
        # ``cwd=None`` means "all projects" — mirrors the real
        # index.query.list_sessions_for_cwd so the prior_sessions_for_cwd
        # this_cwd -> all_projects fallback is exercisable.
        if cwd is None:
            return list(
                conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )
        return list(
            conn.execute(
                "SELECT * FROM sessions WHERE cwd = ? ORDER BY started_at DESC LIMIT ?",
                (cwd, limit),
            ).fetchall()
        )

    fake.search_extractions = search_extractions  # type: ignore[attr-defined]
    fake.search_messages = search_messages  # type: ignore[attr-defined]
    fake.list_sessions_for_cwd = list_sessions_for_cwd  # type: ignore[attr-defined]

    # Also install a stub parent ``index`` package so ``from index import query``
    # resolves without ever touching the real (or absent) `index` module.
    parent = types.ModuleType("index")
    parent.query = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index", parent)
    monkeypatch.setitem(sys.modules, "index.query", fake)
    return fake


def _create_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema mirroring WT-4's `extractions` / `messages` / `sessions`."""
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
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            ts TEXT NOT NULL
        );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            ai_title TEXT,
            last_prompt TEXT,
            started_at TEXT,
            message_count INTEGER
        );
        """
    )


def _seed_corpus(db_path: Path) -> None:
    """Five mixed extractions across two cwds, three kinds, varying scores."""
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    rows = [
        # (id, kind, content, session, cwd, ts, src, score, context)
        (1, "decision", "Use provider-x CPX21 for the relay nodes", "s1",
         "/home/operator/proj-a", ts, "u1", 0.92, json.dumps({})),
        (2, "correction", "no, provider-x is fine — stop suggesting provider-y", "s1",
         "/home/operator/proj-a", ts, "u2", 0.88,
         json.dumps({"rejected_approach": "Switch the relay to provider-y BHS"})),
        (3, "progress", "provider-x relay git.example.com is up and serving traffic", "s2",
         "/home/operator/proj-b", ts, "u3", 0.75, json.dumps({})),
        (4, "domain_fact", "User prefers provider-x for cheap dedicated boxes", "s3",
         "/home/operator/proj-b", ts, "u4", 0.60, json.dumps({})),
        # Decoy: matches nothing
        (5, "decision", "Use Stripe Checkout for billing", "s4",
         "/home/operator/proj-c", ts, "u5", 0.99, json.dumps({})),
    ]
    conn.executemany(
        "INSERT INTO extractions VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("s1", "/home/operator/proj-a", "Set up relay fleet",
         "deploy the relay", ts, 42),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        (1, "s1", "/home/operator/proj-a", "user", "tell me about provider-x", ts),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Tool registration shape
# ---------------------------------------------------------------------------


EXPECTED_TOOLS = {
    "recall",
    "prior_sessions_for_cwd",
    "find_failed_attempts",
    "find_user_preferences",
    "get_session_digest",
    "search_messages",
}


def test_all_expected_tools_registered(server_module):
    tools = asyncio.run(server_module.mcp.list_tools())
    names = {t.name for t in tools}
    assert names >= EXPECTED_TOOLS, f"missing tools: {EXPECTED_TOOLS - names}"


def test_every_tool_has_description_and_schema(server_module):
    tools = asyncio.run(server_module.mcp.list_tools())
    for t in tools:
        assert t.description, f"{t.name} missing description"
        # The description is the model's only hint at *when* to use it — keep
        # it under 2KB (Claude Code truncates) and ideally under 300 chars.
        assert len(t.description) < 2048, f"{t.name} description too long"
        assert t.inputSchema, f"{t.name} missing inputSchema"
        assert "properties" in t.inputSchema


def test_recall_schema_enums(server_module):
    tools = {t.name: t for t in asyncio.run(server_module.mcp.list_tools())}
    recall = tools["recall"]
    props = recall.inputSchema["properties"]
    assert "topic" in props
    assert "kind" in props
    assert "scope" in props
    assert set(props["kind"]["enum"]) == {
        "any", "correction", "decision", "self_correction",
        "progress", "domain_fact", "away_summary",
    }
    assert set(props["scope"]["enum"]) == {"this_cwd", "all_projects"}


def test_search_messages_role_enum(server_module):
    tools = {t.name: t for t in asyncio.run(server_module.mcp.list_tools())}
    props = tools["search_messages"].inputSchema["properties"]
    assert set(props["role"]["enum"]) == {"any", "user", "assistant"}


# ---------------------------------------------------------------------------
# 2. Missing-index degradation
# ---------------------------------------------------------------------------


def test_recall_returns_error_when_db_missing(server_module):
    # No DB file has been created in tmp_db_dir.
    assert not server_module.DB_PATH.exists()
    _, structured = asyncio.run(
        server_module.mcp.call_tool("recall", {"topic": "anything"})
    )
    out = structured["result"]
    assert isinstance(out, list)
    assert out and "error" in out[0]
    assert "not initialized" in out[0]["error"]


def test_prior_sessions_returns_error_when_db_missing(server_module):
    _, structured = asyncio.run(
        server_module.mcp.call_tool("prior_sessions_for_cwd", {})
    )
    out = structured["result"]
    assert out and "error" in out[0]


def test_get_session_digest_returns_error_when_db_missing(server_module):
    # `get_session_digest` returns `dict` (not `list[dict]`), so FastMCP
    # doesn't auto-derive a structured-output schema for it — we read the
    # unstructured text-content payload instead.
    result = asyncio.run(
        server_module.mcp.call_tool("get_session_digest", {"session_id": "x"})
    )
    content = result[0] if isinstance(result, (list, tuple)) else result
    out = json.loads(content.text) if hasattr(content, "text") else content
    assert isinstance(out, dict)
    assert "error" in out


# ---------------------------------------------------------------------------
# 3. End-to-end recall over a synthetic corpus
# ---------------------------------------------------------------------------


def test_recall_ranks_synthetic_corpus(tmp_db_dir, fake_index_query):
    _seed_corpus(tmp_db_dir / "index.db")
    # (Re)import server now that the DB exists and the fake module is in place.
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")

    _, structured = asyncio.run(
        server.mcp.call_tool(
            "recall", {"topic": "provider-x", "scope": "all_projects", "limit": 10}
        )
    )
    hits = structured["result"]
    assert isinstance(hits, list)
    # Four of the five extractions mention 'provider-x'; the Stripe decoy must
    # be excluded by the LIKE filter in the fake index.
    assert len(hits) == 4
    contents = [h["content"] for h in hits]
    assert all("provider-x" in c.lower() for c in contents)
    # Score-descending order: 0.92 > 0.88 > 0.75 > 0.60.
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(0.92)
    # Every hit carries the documented shape.
    for h in hits:
        assert {"content", "session_id", "cwd", "ts", "kind", "score"} <= h.keys()


def test_recall_filters_by_kind(tmp_db_dir, fake_index_query):
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool(
            "recall",
            {"topic": "provider-x", "scope": "all_projects", "kind": "correction"},
        )
    )
    hits = structured["result"]
    assert len(hits) == 1
    assert hits[0]["kind"] == "correction"


def test_recall_scopes_to_cwd(tmp_db_dir, fake_index_query, monkeypatch):
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-a")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool("recall", {"topic": "provider-x", "scope": "this_cwd"})
    )
    hits = structured["result"]
    # proj-a has rows 1, 2 mentioning provider-x.
    assert len(hits) == 2
    assert {h["session_id"] for h in hits} == {"s1"}


def test_find_failed_attempts_returns_paired_rejection(tmp_db_dir, fake_index_query, monkeypatch):
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-a")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool("find_failed_attempts", {"pattern": "provider-x"})
    )
    hits = structured["result"]
    assert len(hits) == 1
    h = hits[0]
    assert h["correction"].startswith("no, provider-x")
    assert h["rejected_approach"] == "Switch the relay to provider-y BHS"
    assert h["session_id"] == "s1"


def test_prior_sessions_for_cwd_lists_sessions(tmp_db_dir, fake_index_query):
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool(
            "prior_sessions_for_cwd", {"cwd": "/home/operator/proj-a"}
        )
    )
    rows = structured["result"]
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "s1"
    assert r["ai_title"] == "Set up relay fleet"
    assert r["message_count"] == 42


def test_prior_sessions_for_cwd_falls_back_when_cwd_unknown(
    tmp_db_dir, fake_index_query, monkeypatch
):
    """No explicit cwd + unknown current cwd -> retry all_projects + _meta."""
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-not-in-index")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool("prior_sessions_for_cwd", {})
    )
    rows = structured["result"]
    # First row is the scope-fallback meta, body carries the proj-a session.
    assert rows and "_meta" in rows[0]
    assert rows[0]["scope_used"] == "all_projects"
    assert any(r.get("session_id") == "s1" for r in rows[1:])


def test_prior_sessions_for_cwd_explicit_cwd_no_fallback(
    tmp_db_dir, fake_index_query, monkeypatch
):
    """An explicit unknown cwd returns empty — no all_projects fallback."""
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool(
            "prior_sessions_for_cwd", {"cwd": "/home/operator/proj-not-in-index"}
        )
    )
    rows = structured["result"]
    # Explicit cwd is honored verbatim: no rows, no _meta fallback marker.
    assert not any("_meta" in r for r in rows)


def test_search_messages_falls_through_to_fts(tmp_db_dir, fake_index_query):
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool(
            "search_messages",
            {"query": "provider-x", "cwd": "/home/operator/proj-a"},
        )
    )
    rows = structured["result"]
    assert len(rows) == 1
    assert rows[0]["text"] == "tell me about provider-x"


def test_get_session_digest_assembles_digest(tmp_db_dir, fake_index_query):
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    result = asyncio.run(
        server.mcp.call_tool("get_session_digest", {"session_id": "s1"})
    )
    content = result[0] if isinstance(result, (list, tuple)) else result
    digest = json.loads(content.text) if hasattr(content, "text") else content
    assert isinstance(digest, dict)
    assert digest["session_id"] == "s1"
    assert digest["ai_title"] == "Set up relay fleet"
    # Two extractions belong to s1: decision (0.92) and correction (0.88).
    assert len(digest["top_decisions"]) == 1
    assert digest["top_decisions"][0]["score"] == pytest.approx(0.92)
    assert len(digest["top_corrections"]) == 1


# ---------------------------------------------------------------------------
# 4. Smart scope fallback (F6)
# ---------------------------------------------------------------------------


def test_recall_falls_back_to_all_projects_when_this_cwd_empty(
    tmp_db_dir, fake_index_query, monkeypatch
):
    """`recall(scope="this_cwd")` from a cwd not in the index must retry
    automatically and surface a `_meta` row so the model knows the broader
    scope was searched."""
    _seed_corpus(tmp_db_dir / "index.db")
    # Point the MCP child at a cwd that has zero indexed extractions.
    monkeypatch.setenv("PWD", "/home/operator/proj-not-in-index")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")

    _, structured = asyncio.run(
        server.mcp.call_tool(
            "recall", {"topic": "provider-x", "scope": "this_cwd", "limit": 10}
        )
    )
    hits = structured["result"]
    assert isinstance(hits, list)
    # The leading row carries the scope-fallback meta.
    assert hits, "expected fallback hits, got empty list"
    meta = hits[0]
    assert "_meta" in meta
    assert meta.get("scope_used") == "all_projects"
    assert meta.get("scope_requested") == "this_cwd"
    # And the remaining rows are the real hits from the broader scope.
    body = hits[1:]
    assert len(body) == 4
    assert all("provider-x" in (h.get("content") or "").lower() for h in body)


def test_recall_no_fallback_meta_when_this_cwd_has_hits(
    tmp_db_dir, fake_index_query, monkeypatch
):
    """When `this_cwd` already yields hits we must NOT prepend the meta row
    — the model treats `_meta` as a 'fell back' signal."""
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-a")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool(
            "recall", {"topic": "provider-x", "scope": "this_cwd", "limit": 10}
        )
    )
    hits = structured["result"]
    assert len(hits) == 2
    for h in hits:
        assert "_meta" not in h


def test_search_messages_falls_back_to_all_projects(
    tmp_db_dir, fake_index_query, monkeypatch
):
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-not-in-index")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool("search_messages", {"query": "provider-x"})
    )
    rows = structured["result"]
    assert rows and "_meta" in rows[0]
    assert rows[0]["scope_used"] == "all_projects"
    # Body contains the seeded message.
    assert any(r.get("text") == "tell me about provider-x" for r in rows[1:])


def test_find_failed_attempts_falls_back_when_cwd_empty(
    tmp_db_dir, fake_index_query, monkeypatch
):
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-not-in-index")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool("find_failed_attempts", {"pattern": "provider-x"})
    )
    hits = structured["result"]
    assert hits and "_meta" in hits[0]
    assert hits[0]["scope_used"] == "all_projects"
    # Body has the rejected_approach extraction.
    assert any(h.get("rejected_approach") == "Switch the relay to provider-y BHS" for h in hits[1:])


def test_find_user_preferences_falls_back_when_this_cwd_empty(
    tmp_db_dir, fake_index_query, monkeypatch
):
    _seed_corpus(tmp_db_dir / "index.db")
    monkeypatch.setenv("PWD", "/home/operator/proj-not-in-index")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    _, structured = asyncio.run(
        server.mcp.call_tool(
            "find_user_preferences", {"scope": "this_cwd", "domain": "provider-x"}
        )
    )
    hits = structured["result"]
    assert hits and "_meta" in hits[0]
    assert hits[0]["scope_used"] == "all_projects"


def test_session_digest_returns_populated_meta(tmp_db_dir, fake_index_query, monkeypatch):
    """`get_session_digest` must surface ai_title / last_prompt / cwd /
    message_count / started_at when F5's `get_session_meta` is available.

    F5 may not have landed on this branch yet; we monkey-patch a stub
    `get_session_meta` onto the fake `index.query` module to simulate it.
    """
    _seed_corpus(tmp_db_dir / "index.db")
    # Stub F5's API: return a dict-shaped meta record for s1.
    started_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    def fake_get_session_meta(conn, session_id):  # noqa: ANN001
        if session_id != "s1":
            return None
        return {
            "session_id": "s1",
            "cwd": "/home/operator/proj-a",
            "ai_title": "F5-supplied title",
            "last_prompt": "deploy the relay",
            "started_at": started_at,
            "message_count": 42,
        }

    fake_index_query.get_session_meta = fake_get_session_meta  # type: ignore[attr-defined]

    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")

    result = asyncio.run(
        server.mcp.call_tool("get_session_digest", {"session_id": "s1"})
    )
    content = result[0] if isinstance(result, (list, tuple)) else result
    digest = json.loads(content.text) if hasattr(content, "text") else content
    assert isinstance(digest, dict)
    # All five canonical meta keys populated from F5's get_session_meta.
    assert digest["ai_title"] == "F5-supplied title"
    assert digest["last_prompt"] == "deploy the relay"
    assert digest["cwd"] == "/home/operator/proj-a"
    assert digest["message_count"] == 42
    assert digest["started_at"]  # ISO-coerced, non-empty
    # And the layered extraction sections still come back.
    assert "top_decisions" in digest
    assert "top_corrections" in digest
    assert "progress_markers" in digest


def test_session_digest_returns_error_when_session_unknown(
    tmp_db_dir, fake_index_query
):
    """No `sessions` row and no messages for the id → clear error payload."""
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    server = importlib.import_module("mcp_server.server")
    result = asyncio.run(
        server.mcp.call_tool(
            "get_session_digest", {"session_id": "no-such-session-id"}
        )
    )
    content = result[0] if isinstance(result, (list, tuple)) else result
    digest = json.loads(content.text) if hasattr(content, "text") else content
    assert isinstance(digest, dict)
    assert "error" in digest
    assert "not in index" in digest["error"]


# ---------------------------------------------------------------------------
# 5. Event emission (observability for `metrics health`)
# ---------------------------------------------------------------------------


def test_mcp_tool_emits_event_on_success(tmp_db_dir, fake_index_query, monkeypatch):
    """Each MCP tool invocation must emit one NDJSON event with elapsed_ms,
    topic_len (when a topic kwarg is present), and hits (when list-shaped)."""
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    tools = importlib.import_module("mcp_server.tools")

    captured: list[tuple[str, dict]] = []

    def fake_emit(event, **kwargs):
        captured.append((event, kwargs))

    monkeypatch.setattr(tools, "_emit", fake_emit)

    result = tools.recall(topic="provider-x", scope="all_projects", limit=10)
    assert isinstance(result, list)
    assert len(captured) == 1, f"expected exactly one event, got {captured}"
    event_name, attrs = captured[0]
    assert event_name == "mcp.recall"
    # elapsed_ms is forwarded as a float
    assert "elapsed_ms" in attrs and isinstance(attrs["elapsed_ms"], float)
    assert attrs["elapsed_ms"] >= 0.0
    # topic="provider-x" → topic_len == 10
    assert attrs["topic_len"] == 10
    # list-shaped result → hits == len(result)
    assert attrs["hits"] == len(result)
    # No raw topic string leaked into the event payload.
    assert "topic" not in attrs
    # No exception was raised → error is None.
    assert attrs.get("error") is None


def test_mcp_tool_emits_event_on_error(tmp_db_dir, fake_index_query, monkeypatch):
    """When the wrapped tool raises, the event must still be emitted with
    `error=<ExcClassName>` and a populated elapsed_ms."""
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    tools = importlib.import_module("mcp_server.tools")

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(tools, "_emit", lambda event, **kw: captured.append((event, kw)))

    class BoomError(RuntimeError):
        pass

    def exploding_get_conn():
        raise BoomError("db kaboom")

    # Force `recall` past the `conn is None` early return by raising from
    # get_conn itself — that propagates out of the tool body.
    monkeypatch.setattr(tools, "get_conn", exploding_get_conn)

    with pytest.raises(BoomError):
        tools.recall(topic="provider-x")

    assert len(captured) == 1
    event_name, attrs = captured[0]
    assert event_name == "mcp.recall"
    assert attrs["error"] == "BoomError"
    assert isinstance(attrs["elapsed_ms"], float)
    assert attrs["elapsed_ms"] >= 0.0
    assert attrs["topic_len"] == 10
    # No `hits` key — result was never assigned a list.
    assert "hits" not in attrs


def test_emit_never_breaks_tool(tmp_db_dir, fake_index_query, monkeypatch):
    """A broken event sink must not affect the tool's return value — the
    _traced wrapper traps emit exceptions so observability can never break
    the host."""
    _seed_corpus(tmp_db_dir / "index.db")
    for mod in ("mcp_server", "mcp_server.server", "mcp_server.tools", "mcp_server.resources"):
        sys.modules.pop(mod, None)
    tools = importlib.import_module("mcp_server.tools")

    def angry_emit(event, **kwargs):
        raise RuntimeError("event sink is on fire")

    monkeypatch.setattr(tools, "_emit", angry_emit)

    # Tool should return normally — recall over the seeded corpus.
    out = tools.recall(topic="provider-x", scope="all_projects", limit=10)
    assert isinstance(out, list)
    assert len(out) == 4
    assert all("provider-x" in (h.get("content") or "").lower() for h in out)
