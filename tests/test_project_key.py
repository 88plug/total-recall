"""Tests for worktree project-key pooling (FIX 1).

Covers:
* :func:`index.paths.project_key` unit behaviour on real corpus paths.
* v4 → v5 schema migration (column add + backfill + idempotency + version).
* Pooling through :func:`index.query.search_extractions` (and the
  ``exact_cwd`` escape hatch).
* Goal-stack pooling through :func:`index.goals`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from index.db import apply_schema, connect
from index.goals import (
    apply_schema as goals_apply_schema,
)
from index.goals import (
    get_active_goal,
    upsert_from_extractions,
)
from index.paths import project_key
from index.query import search_extractions

# ---------------------------------------------------------------------------
# Unit: project_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cwd,expected",
    [
        (
            "/home/andrew/geostrata/.claude/worktrees/wf_63c6fea0-181-1",
            "/home/andrew/geostrata",
        ),
        (
            "/home/andrew/ai-speed/aispeed/.claude/worktrees/wf_24c04719-907-1",
            "/home/andrew/ai-speed/aispeed",
        ),
        (
            "/home/andrew/control-plane-zero-human-companies/.worktrees/"
            "m0-safety-floor/apps/atria-council",
            "/home/andrew/control-plane-zero-human-companies",
        ),
        # No marker -> unchanged (do not over-strip plugin cache paths).
        (
            "/home/andrew/.claude/plugins/cache/88plug/total-recall/2.0.1",
            "/home/andrew/.claude/plugins/cache/88plug/total-recall/2.0.1",
        ),
        (None, None),
        ("/home/x/", "/home/x"),
        ("/", "/"),
        ("/home/x", "/home/x"),
    ],
)
def test_project_key(cwd, expected):
    assert project_key(cwd) == expected


# ---------------------------------------------------------------------------
# Migration: v4 -> v5
# ---------------------------------------------------------------------------

def _build_v4_db(db_path: Path, wt_cwd: str, parent: str) -> None:
    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT,
            git_branch TEXT, role TEXT NOT NULL, kind TEXT, ts INTEGER,
            parent_uuid TEXT, message_uuid TEXT UNIQUE,
            byte_offset INTEGER NOT NULL, source_file TEXT NOT NULL,
            text TEXT, raw_json BLOB,
            source TEXT NOT NULL DEFAULT 'claude_code',
            dedup_superseded_by_source TEXT);
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            session_id TEXT NOT NULL, cwd TEXT, ts INTEGER,
            source_uuid TEXT, score REAL DEFAULT 0.5,
            scope TEXT DEFAULT 'project', context_json TEXT,
            source TEXT NOT NULL DEFAULT 'claude_code',
            dedup_superseded_by_source TEXT,
            UNIQUE(kind, source_uuid));
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', '4');
        """
    )
    raw.execute(
        "INSERT INTO messages(session_id, cwd, role, byte_offset, source_file, "
        "text) VALUES ('s1', ?, 'user', 0, '/tmp/x', 'wt row')",
        (wt_cwd,),
    )
    raw.execute(
        "INSERT INTO messages(session_id, cwd, role, byte_offset, source_file, "
        "text) VALUES ('s2', ?, 'user', 0, '/tmp/y', 'plain row')",
        (parent,),
    )
    raw.execute(
        "INSERT INTO extractions(kind, content, session_id, cwd, source_uuid) "
        "VALUES ('decision', 'wt fact', 's1', ?, 'u1')",
        (wt_cwd,),
    )
    raw.commit()
    raw.close()


def test_schema_v4_to_v5_migration(tmp_path: Path) -> None:
    wt_cwd = "/home/andrew/geostrata/.claude/worktrees/wf_abc-1"
    parent = "/home/andrew/geostrata"
    db_path = tmp_path / "v4.db"
    _build_v4_db(db_path, wt_cwd, parent)

    c = connect(db_path)
    try:
        msg_cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        ext_cols = {r["name"] for r in c.execute("PRAGMA table_info(extractions)")}
        assert "project_key" in msg_cols
        assert "project_key" in ext_cols

        # Worktree cwd collapsed to repo root in backfill.
        pk = c.execute(
            "SELECT project_key FROM messages WHERE text = 'wt row'"
        ).fetchone()["project_key"]
        assert pk == parent
        # Plain row left as-is.
        pk2 = c.execute(
            "SELECT project_key FROM messages WHERE text = 'plain row'"
        ).fetchone()["project_key"]
        assert pk2 == parent
        # Extraction backfilled too.
        epk = c.execute(
            "SELECT project_key FROM extractions WHERE content = 'wt fact'"
        ).fetchone()["project_key"]
        assert epk == parent

        ver = c.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert ver == "5"

        # Idempotent: second apply leaves everything unchanged.
        apply_schema(c)
        ver2 = c.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert ver2 == "5"
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        pk_after = c.execute(
            "SELECT project_key FROM messages WHERE text = 'wt row'"
        ).fetchone()["project_key"]
        assert pk_after == parent
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Pooling: search_extractions
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    return c


def _insert_ext(c, *, content, cwd, source_uuid, kind="note", ts=1_700_000_000):
    c.execute(
        """
        INSERT INTO extractions(
            kind, content, session_id, cwd, ts, source_uuid,
            score, scope, context_json, source, project_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind, content, "s1", cwd, ts, source_uuid, 0.8, "project",
            json.dumps({}), "claude_code", project_key(cwd),
        ),
    )
    c.commit()


def test_search_extractions_pools_worktree_to_parent():
    c = _conn()
    wt = "/home/andrew/geostrata/.claude/worktrees/wf_x-1"
    parent = "/home/andrew/geostrata"
    _insert_ext(c, content="fact from worktree", cwd=wt, source_uuid="u1")

    # Search by parent finds the worktree-scoped extraction.
    hits = search_extractions(c, cwd=parent)
    assert any(h.content == "fact from worktree" for h in hits)


def test_search_extractions_pools_parent_to_worktree():
    c = _conn()
    wt = "/home/andrew/geostrata/.claude/worktrees/wf_x-1"
    parent = "/home/andrew/geostrata"
    _insert_ext(c, content="fact from parent", cwd=parent, source_uuid="u1")

    # Search by worktree cwd finds the parent-scoped extraction (reverse).
    hits = search_extractions(c, cwd=wt)
    assert any(h.content == "fact from parent" for h in hits)


def test_search_extractions_exact_cwd_does_not_pool():
    c = _conn()
    wt = "/home/andrew/geostrata/.claude/worktrees/wf_x-1"
    parent = "/home/andrew/geostrata"
    _insert_ext(c, content="fact from worktree", cwd=wt, source_uuid="u1")

    # exact_cwd=True keeps per-worktree precision: parent query misses it.
    hits = search_extractions(c, cwd=parent, exact_cwd=True)
    assert not any(h.content == "fact from worktree" for h in hits)
    # But querying the exact worktree cwd still finds it.
    hits2 = search_extractions(c, cwd=wt, exact_cwd=True)
    assert any(h.content == "fact from worktree" for h in hits2)


def test_search_extractions_different_projects_never_pool():
    c = _conn()
    _insert_ext(
        c, content="geostrata fact",
        cwd="/home/andrew/geostrata/.claude/worktrees/wf_a-1", source_uuid="u1",
    )
    _insert_ext(
        c, content="aispeed fact",
        cwd="/home/andrew/ai-speed/aispeed/.claude/worktrees/wf_b-1",
        source_uuid="u2",
    )
    hits = search_extractions(c, cwd="/home/andrew/geostrata")
    contents = {h.content for h in hits}
    assert "geostrata fact" in contents
    assert "aispeed fact" not in contents


# ---------------------------------------------------------------------------
# Pooling: goals
# ---------------------------------------------------------------------------

def _goal_ext(content, cwd, ts=1_700_000_000):
    return type("E", (), {
        "kind": "goal", "content": content,
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc),
        "cwd": cwd, "session_id": "s1", "source_uuid": f"u-{ts}",
        "score": 0.8, "scope": "project", "context": {},
    })()


def test_goal_pooling_worktree_retrievable_via_parent():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    goals_apply_schema(c)
    wt = "/home/andrew/geostrata/.claude/worktrees/wf_x-1"
    parent = "/home/andrew/geostrata"
    n, _ = upsert_from_extractions(c, [_goal_ext("ship the thing", wt)])
    assert n == 1
    # Stored project is the collapsed root.
    proj = c.execute("SELECT project FROM goal_stack").fetchone()["project"]
    assert proj == parent
    # Retrievable via parent cwd.
    g = get_active_goal(c, parent)
    assert g is not None
    assert g.goal_text == "ship the thing"
    # And via the worktree cwd (read-side normalization).
    g2 = get_active_goal(c, wt)
    assert g2 is not None and g2.goal_text == "ship the thing"


def test_goals_apply_schema_backfills_legacy_worktree_project():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    goals_apply_schema(c)
    wt = "/home/andrew/geostrata/.claude/worktrees/wf_x-1"
    parent = "/home/andrew/geostrata"
    # Simulate a legacy row written before normalization (raw worktree project).
    c.execute(
        "INSERT INTO goal_stack(project, goal_text, declared_ts, "
        "last_progress_ts, status) VALUES (?, 'legacy goal', 1, 1, 'active')",
        (wt,),
    )
    c.commit()
    # Re-apply schema -> idempotent backfill collapses it.
    goals_apply_schema(c)
    proj = c.execute(
        "SELECT project FROM goal_stack WHERE goal_text = 'legacy goal'"
    ).fetchone()["project"]
    assert proj == parent
    # Idempotent second run.
    goals_apply_schema(c)
    proj2 = c.execute(
        "SELECT project FROM goal_stack WHERE goal_text = 'legacy goal'"
    ).fetchone()["project"]
    assert proj2 == parent
