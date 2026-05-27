"""Compute the per-scope context payload to inject after a scope shift.

Design contract
---------------
This module is called by the ``UserPromptSubmit`` hook layer **only after**
:func:`hooks.lib.scope_detect.detect_scope_shift` has fired. The model
already has the global ``CLAUDE.md`` in context, so we never re-emit
global rules. We only inject what is *new* for the destination scope:

* the destination scope's active goal (if any),
* standing decisions scoped to ``to_scope`` that aren't already valid
  in ``from_scope`` (the model still has those),
* bans relevant to the destination scope,
* a short list of machines that belong to the scope.

Everything is best-effort. If an ``index.*`` module is missing or its
query API doesn't yet exist (sibling worktrees may not have merged), we
swallow the import/AttributeError and skip the section. The hook caller
treats an empty return value as "nothing to inject" — never as an error.

Hard char cap defaults to 2000 (~500 tokens). The priority order when
the budget is tight is:

    active_goal > standing_decisions > bans > machines

If the assembled payload is below ``MIN_INJECTION_CHARS`` we return ``""``
rather than inject a near-empty block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

MAX_CHARS = 2000
MIN_INJECTION_CHARS = 50  # below this, don't bother


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_scope_delta(
    db_path: Path,
    from_scope: Optional[str],
    to_scope: str,
    max_chars: int = MAX_CHARS,
) -> str:
    """Return a markdown-formatted delta payload for injecting after scope shift.

    Logic:

    1. Build to_scope context (active goal, decisions, bans, machines).
    2. If from_scope given, subtract anything that's in both (the model
       already has it in its working context).
    3. Truncate to ``max_chars``; priority order:
       ``active_goal > standing_decisions > bans > machines``.
    4. If the assembled body is below :data:`MIN_INJECTION_CHARS` return
       an empty string — the hook caller treats that as "skip".
    """
    sections: list[tuple[str, str]] = []

    # 1. Active goal for to_scope's cwd ---------------------------------------
    try:
        from index.goals import get_active_goal_for_cwd  # type: ignore

        cwd = _scope_to_canonical_cwd(to_scope)
        if cwd:
            goal = get_active_goal_for_cwd(_open_conn(db_path), cwd)
            if goal:
                sections.append(("active_goal", _format_goal(goal)))
    except Exception:
        # Fall back to the project-keyed API which actually exists today.
        try:
            from index.goals import get_active_goal  # type: ignore

            cwd = _scope_to_canonical_cwd(to_scope)
            if cwd:
                goal = get_active_goal(_open_conn(db_path), cwd)
                if goal is not None:
                    goal_dict = goal.to_dict() if hasattr(goal, "to_dict") else dict(goal)
                    sections.append(("active_goal", _format_goal(goal_dict)))
        except Exception:
            pass

    # 2. Standing decisions ---------------------------------------------------
    try:
        from index.decisions import list_decisions  # type: ignore

        decisions = list_decisions(_open_conn(db_path), scope=to_scope, limit=5)
        if from_scope:
            existing = {
                d["topic"]
                for d in list_decisions(
                    _open_conn(db_path), scope=from_scope, limit=10
                )
            }
            decisions = [d for d in decisions if d.get("topic") not in existing]
        if decisions:
            sections.append(("standing_decisions", _format_decisions(decisions)))
    except Exception:
        pass

    # 3. Bans relevant to the destination scope -------------------------------
    try:
        from index.bans import list_bans_for_scope  # type: ignore

        bans = list_bans_for_scope(_open_conn(db_path), to_scope, limit=3)
        if bans:
            sections.append(("bans", _format_bans(bans)))
    except Exception:
        pass

    # 4. Relevant machines from the ontology ---------------------------------
    try:
        from index.ontology import machines_for_scope  # type: ignore

        machines = machines_for_scope(_open_conn(db_path), to_scope, limit=3)
        if machines:
            sections.append(("machines", _format_machines(machines)))
    except Exception:
        pass

    if not sections:
        return ""

    return _assemble(sections, from_scope, to_scope, max_chars)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _assemble(
    sections: list[tuple[str, str]],
    from_scope: Optional[str],
    to_scope: str,
    max_chars: int,
) -> str:
    """Glue sections together, respecting priority order and char budget."""
    if from_scope:
        header = f"[scope-shift {from_scope} -> {to_scope}]\n\n"
    else:
        header = f"[scope-shift -> {to_scope}]\n\n"

    remaining = max_chars - len(header)
    body = ""
    for name, content in sections:  # already in priority order
        block = f"## {name}\n{content}\n\n"
        if len(block) <= remaining:
            body += block
            remaining -= len(block)
            continue
        # Truncate to fit
        scaffold = f"## {name}\n\n\n"
        available = remaining - len(scaffold) - 1  # -1 for the ellipsis
        if available > 80:
            body += f"## {name}\n{content[:available]}…\n\n"
        break

    if len(body) < MIN_INJECTION_CHARS:
        return ""
    return header + body


def _open_conn(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _scope_to_canonical_cwd(scope: str, db_path: Optional[Path] = None) -> Optional[str]:
    """Resolve a scope name to its canonical cwd via the ``projects`` table.

    Lookup order:
    1. Exact match on ``display_name`` (case-insensitive).
    2. Basename match: ``scope`` equals the last path segment of stored cwd.

    Both lookups query the live DB so the result is correct for any operator,
    not just the original author.  Falls back to ``None`` when the DB is
    missing, the table is empty, or no project matches the scope.  The
    ``db_path`` argument is accepted (and forwarded by callers that already
    resolved it) but defaults to the same path resolution as ``index/db.py``.
    """
    import sqlite3 as _sqlite3
    import os as _os
    from pathlib import Path as _Path

    # Resolve the DB path the same way index/db.py does.
    _db = db_path
    if _db is None:
        base_env = _os.environ.get("CLAUDE_PLUGIN_DATA")
        if base_env:
            _db = _Path(base_env).expanduser() / "total-recall" / "index.db"
        else:
            _db = _Path("~/.local/share/total-recall/index.db").expanduser()

    if not _db.exists():
        return None

    try:
        uri = f"file:{_db}?mode=ro"
        conn = _sqlite3.connect(uri, uri=True, timeout=3.0, isolation_level=None)
        conn.row_factory = _sqlite3.Row
    except Exception:
        return None

    try:
        scope_lower = scope.strip().lower()

        # 1. Exact display_name match (case-insensitive)
        row = conn.execute(
            "SELECT cwd FROM projects WHERE LOWER(COALESCE(display_name,'')) = ?",
            (scope_lower,),
        ).fetchone()
        if row:
            return row["cwd"]

        # 2. Basename match: last path segment of stored cwd == scope
        #    We fetch all cwds and filter in Python (no REGEXP in SQLite by default).
        rows = conn.execute("SELECT cwd FROM projects").fetchall()
        for r in rows:
            cwd = r["cwd"] or ""
            parts = [p for p in cwd.rstrip("/").split("/") if p]
            if parts and parts[-1].lower() == scope_lower:
                return cwd

        return None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Formatters — each returns plain markdown lines (no surrounding header).
# Robust to missing keys: index modules differ in column names across worktrees.
# ---------------------------------------------------------------------------


def _g(d: Any, *keys: str, default: str = "") -> str:
    """Pull the first non-empty key from a dict-like row."""
    if d is None:
        return default
    for k in keys:
        try:
            v = d[k]
        except (KeyError, TypeError, IndexError):
            v = getattr(d, k, None)
        if v not in (None, ""):
            return str(v)
    return default


def _format_goal(g: Any) -> str:
    text = _g(g, "goal_text", "text", "goal", default="(unnamed goal)")
    status = _g(g, "status", default="active")
    project = _g(g, "project", "cwd")
    parts = [f"- {text}"]
    meta = []
    if status:
        meta.append(status)
    if project:
        meta.append(project)
    if meta:
        parts.append(f"  ({', '.join(meta)})")
    return "\n".join(parts)


def _format_decisions(rows: list[Any]) -> str:
    out = []
    for r in rows:
        topic = _g(r, "topic", default="?")
        chose = _g(r, "chose", "choice", default="")
        text = _g(r, "ban_text", "decision_text", "text", default="")
        if chose:
            out.append(f"- {topic}: chose {chose}")
        elif text:
            # Trim long decision strings to keep the block compact.
            snippet = text if len(text) <= 120 else text[:117] + "..."
            out.append(f"- {topic}: {snippet}")
        else:
            out.append(f"- {topic}")
    return "\n".join(out)


def _format_bans(rows: list[Any]) -> str:
    out = []
    for r in rows:
        thing = _g(r, "banned_thing", "thing", default="?")
        strength = _g(r, "ban_strength", "strength", default="")
        text = _g(r, "ban_text", "text", default="")
        suffix = f" — {text}" if text else ""
        prefix = f"[{strength}] " if strength else ""
        out.append(f"- {prefix}{thing}{suffix}")
    return "\n".join(out)


def _format_machines(rows: list[Any]) -> str:
    out = []
    for r in rows:
        host = _g(r, "hostname", "name", default="?")
        role = _g(r, "role", default="")
        ip = _g(r, "public_ip", "tailscale_ip", "lan_ip", default="")
        bits = [host]
        if role:
            bits.append(f"({role})")
        if ip:
            bits.append(ip)
        out.append("- " + " ".join(bits))
    return "\n".join(out)
