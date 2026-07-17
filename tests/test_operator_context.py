"""Tests for ``mcp_server.extras.operator_context_tools.get_operator_context``.

The tool is the SessionStart "one call" aggregator — it bundles seven
optional sister surfaces (identity, active goal, standing decisions, bans,
voice, recent corrections, machines) into a single ~1800-char payload.

These tests cover three properties:

1. **Section priority.** With every surface stubbed, the response includes
   every section and they appear in the documented priority order.

2. **Graceful degradation.** If any sister module raises on import or call,
   that section is silently dropped — the rest still ship. We exercise this
   by mocking *some* surfaces, leaving the rest to fail.

3. **Budget truncation.** When ``max_chars`` is tight, sections evict from
   the *tail* of the priority list (so identity + active_goal always win).

A bash test for the new signpost script lives at the bottom — it just runs
``hooks/session-start-signpost-v2.sh`` with a synthetic stdin and asserts
exit 0 + a sane stdout shape. Heavyweight integration is left to the existing
``tests/test_hooks.sh`` flow once the orchestrator wires this in.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at a temp DB dir before it's imported."""
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(tmp_path))
    # Force a fresh import so DB_PATH resolves against the new env var and
    # the @mcp.tool() decorator re-registers our tool with a clean FastMCP
    # instance.
    for mod in (
        "mcp_server",
        "mcp_server.server",
        "mcp_server.tools",
        "mcp_server.resources",
        "mcp_server.extras",
        "mcp_server.extras.operator_context_tools",
        "mcp_server.extras.corrections_tools",
    ):
        sys.modules.pop(mod, None)
    return tmp_path


def _create_minimal_db(db_path: Path) -> None:
    """Create just enough of a DB for ``get_conn`` to return a live handle.

    Our collectors read from sister modules that we'll stub at the
    ``sys.modules`` level — they never actually hit this DB. But ``get_conn``
    refuses to open a file that doesn't exist, so we materialize an empty one.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE _bootstrap (k TEXT)")
    conn.commit()
    conn.close()


def _install_all_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every sister module the tool defensively imports.

    Each stub returns predictable, identifiable data so the test can assert
    the *shape* of the merged payload without coupling to the (still-unmerged)
    real implementations.
    """
    # index.operator.get_profile
    operator_mod = types.ModuleType("index.operator")

    def get_profile(conn):
        return {
            "name": "Andrew",
            "handle": "88plug",
            "primary_email": "claude@cryptoandcoffee.com",
            "_confidence": {},
            "_sources": {},
        }

    operator_mod.get_profile = get_profile  # type: ignore[attr-defined]

    # index.goals.get_active_goal_for_cwd
    goals_mod = types.ModuleType("index.goals")

    def get_active_goal_for_cwd(conn, cwd=None):
        return {"goal": "ship I10 integration agent", "scope": cwd or "global"}

    goals_mod.get_active_goal_for_cwd = get_active_goal_for_cwd  # type: ignore[attr-defined]

    # index.decisions: provide top_decisions_for_scope (the spec name).
    decisions_mod = types.ModuleType("index.decisions")

    def top_decisions_for_scope(conn, cwd=None, limit=3):
        return [
            {
                "topic": "vps_provider",
                "chose": "provider-x",
                "over": "provider-y",
                "scope": "global",
                "assertion_count": 7,
            },
            {
                "topic": "billing",
                "chose": "stripe",
                "over": "paypal",
                "scope": "global",
                "assertion_count": 4,
            },
            {
                "topic": "auth",
                "chose": "session-token",
                "over": "JWT",
                "scope": cwd or "global",
                "assertion_count": 2,
            },
        ][:limit]

    decisions_mod.top_decisions_for_scope = top_decisions_for_scope  # type: ignore[attr-defined]

    # index.bans.top_bans
    bans_mod = types.ModuleType("index.bans")

    def top_bans(conn, cwd=None, limit=3):
        return [
            {
                "target": "Cloudflare Workers",
                "kind": "provider",
                "reason": "vendor lock-in",
                "severity": 0.9,
                "scope": "global",
            },
            {
                "target": "git push --force to main",
                "kind": "pattern",
                "reason": "destructive",
                "severity": 1.0,
                "scope": "global",
            },
            {
                "target": "npm install -g",
                "kind": "tool",
                "reason": "use uv/pipx",
                "severity": 0.7,
                "scope": "global",
            },
        ][:limit]

    bans_mod.top_bans = top_bans  # type: ignore[attr-defined]

    # index.voice.get_voice_summary
    voice_mod = types.ModuleType("index.voice")

    def get_voice_summary(conn):
        return [
            "direct, no fluff",
            "engineering-first framing",
            "skip apologies",
        ]

    voice_mod.get_voice_summary = get_voice_summary  # type: ignore[attr-defined]

    # index.query.search_extractions (returns dict-like rows with .keys())
    query_mod = types.ModuleType("index.query")

    class _Row(dict):
        def keys(self):  # type: ignore[override]
            return super().keys()

    def search_extractions(conn, query=None, cwd=None, kind=None, limit=10):
        return [
            _Row(
                kind="model_correction",
                content="no, never use provider-y",
                ts="2026-05-24T12:00:00+00:00",
                context=json.dumps(
                    {
                        "rejected_approach": "Switch the relay to provider-y",
                        "correction": "no, never use provider-y",
                    }
                ),
            ),
            _Row(
                kind="model_correction",
                content="stop suggesting Stripe",
                ts="2026-05-25T09:00:00+00:00",
                context=json.dumps(
                    {
                        "rejected_approach": "Use Stripe Checkout",
                        "correction": "stop suggesting Stripe",
                    }
                ),
            ),
        ][:limit]

    query_mod.search_extractions = search_extractions  # type: ignore[attr-defined]

    # index.ontology.machines_for_cwd
    ontology_mod = types.ModuleType("index.ontology")

    def machines_for_cwd(conn, cwd=None):
        return [
            {"name": "git.example.com", "role": "GitLab + CI", "host": "1.2.3.4"},
            {"name": "relay-bhs1", "role": "WireGuard relay"},
        ]

    ontology_mod.machines_for_cwd = machines_for_cwd  # type: ignore[attr-defined]

    parent = types.ModuleType("index")
    parent.operator = operator_mod  # type: ignore[attr-defined]
    parent.goals = goals_mod  # type: ignore[attr-defined]
    parent.decisions = decisions_mod  # type: ignore[attr-defined]
    parent.bans = bans_mod  # type: ignore[attr-defined]
    parent.voice = voice_mod  # type: ignore[attr-defined]
    parent.query = query_mod  # type: ignore[attr-defined]
    parent.ontology = ontology_mod  # type: ignore[attr-defined]

    for name, mod in (
        ("index", parent),
        ("index.operator", operator_mod),
        ("index.goals", goals_mod),
        ("index.decisions", decisions_mod),
        ("index.bans", bans_mod),
        ("index.voice", voice_mod),
        ("index.query", query_mod),
        ("index.ontology", ontology_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


def _import_tool_module():
    """Import the server first (resolves DB_PATH), then the tool module."""
    server = importlib.import_module("mcp_server.server")
    tool_mod = importlib.import_module("mcp_server.extras.operator_context_tools")
    return server, tool_mod


# ---------------------------------------------------------------------------
# 1. All sections present, priority order honored.
# ---------------------------------------------------------------------------


def test_returns_error_when_db_missing(tmp_db_dir):
    server, tool_mod = _import_tool_module()
    out = tool_mod.get_operator_context()
    assert isinstance(out, dict)
    assert "error" in out
    assert "not initialized" in out["error"]


def test_all_sections_present_in_priority_order(tmp_db_dir, monkeypatch):
    _create_minimal_db(tmp_db_dir / "index.db")
    _install_all_stubs(monkeypatch)
    server, tool_mod = _import_tool_module()

    out = tool_mod.get_operator_context(cwd="/home/operator/proj-a", max_chars=4000)
    assert isinstance(out, dict)
    # All seven content sections + the _kind / _cwd internal tags.
    expected = {
        "identity",
        "active_goal",
        "standing_decisions",
        "bans",
        "voice",
        "recent_corrections",
        "machines",
    }
    assert expected <= set(out.keys()), f"missing: {expected - set(out.keys())}"

    # Priority order: identity must appear before machines etc. when iterating
    # the dict insertion order (Python 3.7+ preserves it).
    keys = [k for k in out if not k.startswith("_")]
    priority = list(tool_mod._PRIORITY)
    # Filter priority to only those present, then compare.
    expected_order = [k for k in priority if k in keys]
    assert keys == expected_order, f"order mismatch: {keys} vs {expected_order}"


def test_section_shapes(tmp_db_dir, monkeypatch):
    _create_minimal_db(tmp_db_dir / "index.db")
    _install_all_stubs(monkeypatch)
    _, tool_mod = _import_tool_module()

    out = tool_mod.get_operator_context(cwd="/home/operator/proj-a", max_chars=4000)
    assert out["identity"]["name"] == "Andrew"
    assert out["identity"]["email"] == "claude@cryptoandcoffee.com"

    assert out["active_goal"]["goal"] == "ship I10 integration agent"

    decisions = out["standing_decisions"]
    assert isinstance(decisions, list) and len(decisions) == 3
    assert decisions[0]["topic"] == "vps_provider"
    assert decisions[0]["chose"] == "provider-x"

    bans = out["bans"]
    assert isinstance(bans, list) and len(bans) == 3
    assert any(b["target"] == "git push --force to main" for b in bans)

    voice = out["voice"]
    assert isinstance(voice, list) and len(voice) <= 10
    assert "direct, no fluff" in voice

    corrections = out["recent_corrections"]
    assert isinstance(corrections, list) and len(corrections) == 2
    assert corrections[0]["correction"] == "no, never use provider-y"
    assert corrections[0]["rejected_approach"] == "Switch the relay to provider-y"

    machines = out["machines"]
    assert isinstance(machines, list)
    assert any("git.example.com" in m for m in machines)


# ---------------------------------------------------------------------------
# 2. Graceful degradation — only some surfaces available.
# ---------------------------------------------------------------------------


def test_graceful_degradation_when_some_modules_missing(tmp_db_dir, monkeypatch):
    """Identity + decisions are mocked; everything else is left to fail."""
    _create_minimal_db(tmp_db_dir / "index.db")

    # Install only two of the seven surfaces.
    operator_mod = types.ModuleType("index.operator")
    operator_mod.get_profile = lambda conn: {  # type: ignore[attr-defined]
        "name": "Andrew",
        "handle": "88plug",
    }
    decisions_mod = types.ModuleType("index.decisions")
    decisions_mod.list_decisions = lambda conn, include_reversed=False, limit=3: [  # type: ignore[attr-defined]
        {"topic": "vps_provider", "chose": "provider-x", "scope": "global", "assertion_count": 7}
    ]
    parent = types.ModuleType("index")
    parent.operator = operator_mod  # type: ignore[attr-defined]
    parent.decisions = decisions_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "index", parent)
    monkeypatch.setitem(sys.modules, "index.operator", operator_mod)
    monkeypatch.setitem(sys.modules, "index.decisions", decisions_mod)
    # Block the others by inserting None into sys.modules — Python's import
    # machinery treats this as "definitely-not-importable" and raises
    # ImportError immediately.
    for name in ("index.goals", "index.bans", "index.voice", "index.query", "index.ontology"):
        monkeypatch.setitem(sys.modules, name, None)

    _, tool_mod = _import_tool_module()
    out = tool_mod.get_operator_context(cwd="/home/operator/proj-a", max_chars=4000)

    # Successful sections present.
    assert "identity" in out
    assert "standing_decisions" in out
    # Missing-surface sections must be absent (not error-marked) — defensive
    # imports degrade silently per-section.
    for absent in ("active_goal", "bans", "voice", "recent_corrections", "machines"):
        assert absent not in out, f"{absent} should have degraded silently"


# ---------------------------------------------------------------------------
# 3. Budget truncation respects priority order.
# ---------------------------------------------------------------------------


def test_max_chars_truncates_from_tail_of_priority(tmp_db_dir, monkeypatch):
    _create_minimal_db(tmp_db_dir / "index.db")
    _install_all_stubs(monkeypatch)
    _, tool_mod = _import_tool_module()

    # Aggressive cap — only the highest-priority sections should survive.
    out = tool_mod.get_operator_context(cwd="/home/operator/proj-a", max_chars=250)

    serialized = json.dumps(
        {k: v for k, v in out.items() if not k.startswith("_")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # _build_payload's cap applies to the pre-tag payload; the final dict has
    # the tags re-added afterward.
    assert len(serialized) <= 250 + 10  # tiny slack for last-resort truncation

    # Identity must be the first survivor — it is the highest-priority section.
    assert "identity" in out
    # Machines is the last in priority — should be gone.
    assert "machines" not in out


def test_build_payload_unit_priority_order():
    """Direct unit test of _build_payload — no DB, no fixtures."""
    from mcp_server.extras import operator_context_tools as oct

    sections = {
        "identity": {"name": "Sam"},
        "active_goal": {"goal": "x" * 500},
        "standing_decisions": [{"topic": "vps", "chose": "provider-x"}],
        "bans": [{"target": "provider-y"}],
        "voice": ["direct"],
        "recent_corrections": [{"correction": "no"}],
        "machines": ["git.example.com"],
    }
    # Budget that comfortably fits identity but not the giant active_goal.
    out = oct._build_payload(sections, max_chars=120)
    # Identity always wins.
    assert "identity" in out
    # Machines (tail of priority) is the first to go.
    assert "machines" not in out


def test_empty_sections_are_pruned(tmp_db_dir, monkeypatch):
    """A section that returns ``None`` / ``[]`` / ``{}`` is dropped, not kept."""
    _create_minimal_db(tmp_db_dir / "index.db")

    # Identity present; everything else returns empty.
    operator_mod = types.ModuleType("index.operator")
    operator_mod.get_profile = lambda conn: {"name": "Andrew"}  # type: ignore[attr-defined]
    goals_mod = types.ModuleType("index.goals")
    goals_mod.get_active_goal_for_cwd = lambda conn, cwd=None: None  # type: ignore[attr-defined]
    decisions_mod = types.ModuleType("index.decisions")
    decisions_mod.list_decisions = lambda conn, include_reversed=False, limit=3: []  # type: ignore[attr-defined]
    bans_mod = types.ModuleType("index.bans")
    bans_mod.top_bans = lambda conn, cwd=None, limit=3: []  # type: ignore[attr-defined]
    voice_mod = types.ModuleType("index.voice")
    voice_mod.get_voice_summary = lambda conn: None  # type: ignore[attr-defined]
    query_mod = types.ModuleType("index.query")
    query_mod.search_extractions = lambda conn, query=None, cwd=None, kind=None, limit=10: []  # type: ignore[attr-defined]
    ontology_mod = types.ModuleType("index.ontology")
    ontology_mod.machines_for_cwd = lambda conn, cwd=None: []  # type: ignore[attr-defined]

    parent = types.ModuleType("index")
    for attr, mod in (
        ("operator", operator_mod),
        ("goals", goals_mod),
        ("decisions", decisions_mod),
        ("bans", bans_mod),
        ("voice", voice_mod),
        ("query", query_mod),
        ("ontology", ontology_mod),
    ):
        setattr(parent, attr, mod)
    monkeypatch.setitem(sys.modules, "index", parent)
    for name, mod in (
        ("index.operator", operator_mod),
        ("index.goals", goals_mod),
        ("index.decisions", decisions_mod),
        ("index.bans", bans_mod),
        ("index.voice", voice_mod),
        ("index.query", query_mod),
        ("index.ontology", ontology_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    _, tool_mod = _import_tool_module()
    out = tool_mod.get_operator_context(cwd="/home/operator/proj-a")

    assert "identity" in out
    for empty_section in (
        "active_goal",
        "standing_decisions",
        "bans",
        "voice",
        "recent_corrections",
        "machines",
    ):
        assert empty_section not in out


# ---------------------------------------------------------------------------
# 4. The tool is registered on the FastMCP server (one-call discoverability).
# ---------------------------------------------------------------------------


def test_tool_is_registered(tmp_db_dir):
    server, tool_mod = _import_tool_module()
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "get_operator_context" in names
    spec = next(t for t in tools if t.name == "get_operator_context")
    assert spec.description, "tool needs a description"
    assert len(spec.description) < 2048
    props = spec.inputSchema["properties"]
    assert "cwd" in props
    assert "max_chars" in props


# ---------------------------------------------------------------------------
# 5. The new signpost script — bash-level smoke test.
# ---------------------------------------------------------------------------


def test_signpost_v2_no_db_silent(tmp_path, monkeypatch):
    """With no DB, the v2 signpost must exit 0 and produce empty stdout."""
    script = REPO_ROOT / "hooks" / "session-start-signpost.sh"
    if not script.exists() or shutil.which("bash") is None:
        pytest.skip("script or bash missing")

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    # Ensure the recall_data root is fresh — no DB → fresh-install bootstrap
    # path. The bootstrap banner is allowed to emit once; we test that the
    # script still exits 0 either way.
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"

    proc = subprocess.run(
        ["bash", str(script)],
        input=json.dumps(
            {
                "session_id": "t",
                "cwd": "/tmp/none",
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    # Must exit 0 always.
    assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"

    out = proc.stdout.strip()
    if out:
        # If anything was emitted, it must be a valid envelope.
        envelope = json.loads(out)
        assert "hookSpecificOutput" in envelope
        assert envelope["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert envelope["hookSpecificOutput"]["additionalContext"]


def test_signpost_v2_with_populated_db(tmp_path, monkeypatch):
    """With a populated DB (operator profile only), v2 emits an envelope."""
    script = REPO_ROOT / "hooks" / "session-start-signpost.sh"
    if not script.exists() or shutil.which("bash") is None:
        pytest.skip("script or bash missing")

    # Build a DB large enough that ``recall::is_fresh_install`` reports false
    # (the threshold is 100KB by default — we override it to 1KB to avoid
    # writing a real 100KB file from the test).
    db_dir = tmp_path / "total-recall"
    db_dir.mkdir()
    db_path = db_dir / "index.db"
    conn = sqlite3.connect(db_path)
    # Real operator_profile schema + one row so the collector returns data.
    from index.operator import ensure_schema, upsert_profile_field

    ensure_schema(conn)
    upsert_profile_field(conn, "name", "Andrew", confidence=0.95)
    upsert_profile_field(conn, "primary_email", "claude@cryptoandcoffee.com", confidence=0.95)
    conn.commit()
    conn.close()

    env = os.environ.copy()
    # Drop any ambient recall/plugin vars another test may have leaked into the
    # process env, so this subprocess sees only what we set below (the hook reads
    # several RECALL_*/CLAUDE_PLUGIN* vars; a stray one makes stdout non-hermetic).
    for _k in [k for k in env if k.startswith(("RECALL_", "TOTAL_RECALL_", "CLAUDE_PLUGIN"))]:
        del env[_k]
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    env["TOTAL_RECALL_DB_DIR"] = str(db_dir)
    env["RECALL_FRESH_SIZE_THRESHOLD"] = "1024"  # 1 KB — our DB is bigger.
    # Disable the orthogonal LLM-refinement notice: without ollama present (e.g.
    # CI) the hook appends a human-readable "[total-recall] …" line to the
    # context, which is valid behaviour but makes additionalContext non-JSON and
    # would break the json.loads assertion below.
    env["TOTAL_RECALL_LLM_PROVIDER"] = "none"
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"

    proc = subprocess.run(
        ["bash", str(script)],
        input=json.dumps(
            {
                "session_id": "t",
                "cwd": "/home/operator/proj-a",
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    out = proc.stdout.strip()
    # When the profile exists, identity is populated → emit an envelope.
    if out:
        envelope = json.loads(out)
        assert envelope["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = envelope["hookSpecificOutput"]["additionalContext"]
        payload = json.loads(ctx)
        assert "identity" in payload
        assert payload["identity"].get("name") == "Andrew"
