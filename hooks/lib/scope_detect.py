"""Scope detection for total-recall hooks.

Scope is derived from the operator's own data — the ``projects`` and
``vocabulary`` tables in the index DB — rather than any hardcoded identity.
When the DB is unavailable (hook cold-start, first run, missing index) we fall
back to a pure string heuristic: the last path segment of the cwd, normalized
to lowercase with spaces replaced by hyphens. This is generic and correct for
any installer.

Public API (stable — tests and callers depend on these names):
    infer_scope(cwd)          -> Optional[str]
    score_scopes(text)        -> dict[str, int]
    detect_scope_shift(...)   -> Optional[ScopeShift]
    dominant_scope(prompts)   -> Optional[str]
    SCOPE_KEYWORDS            -> dict[str, list[str]]  (built dynamically, may be empty)
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# DB access helpers — read-only, fast, never crash
# ---------------------------------------------------------------------------

def _db_path() -> Path | None:
    """Resolve the index DB path the same way index/db.py does."""
    base_env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if base_env:
        base = Path(base_env).expanduser() / "total-recall"
    else:
        base = Path("~/.local/share/total-recall").expanduser()
    p = base / "index.db"
    return p if p.exists() else None


def _open_ro() -> sqlite3.Connection | None:
    """Open the index DB read-only.  Returns None if missing or unreadable."""
    p = _db_path()
    if p is None:
        return None
    try:
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _projects_table(conn: sqlite3.Connection) -> list[dict]:
    """Return all rows from the projects table, or [] on any error."""
    try:
        rows = conn.execute(
            "SELECT cwd, display_name FROM projects ORDER BY last_active_ts DESC NULLS LAST"
        ).fetchall()
        return [{"cwd": r["cwd"], "display_name": r["display_name"]} for r in rows]
    except Exception:
        return []


def _vocab_for_cwd(conn: sqlite3.Connection, cwd: str, limit: int = 10) -> list[str]:
    """Return high-frequency vocabulary terms whose definition mentions this cwd.

    The vocabulary table doesn't carry a cwd FK so we use a text search on the
    definition column as a best-effort proxy.  Falls back to [] on any error.
    """
    try:
        basename = Path(cwd).name.lower()
        rows = conn.execute(
            "SELECT term FROM vocabulary "
            "WHERE LOWER(definition) LIKE ? OR LOWER(category) LIKE ? "
            "ORDER BY frequency DESC LIMIT ?",
            (f"%{basename}%", f"%{basename}%", limit),
        ).fetchall()
        return [r["term"] for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Scope keyword table — built dynamically from the index
# ---------------------------------------------------------------------------

def _build_scope_keywords() -> dict[str, list[str]]:
    """Construct keyword hints for every known project from live DB data.

    For each project row we derive:
    - the cwd basename (last path segment, lowercased)
    - every dash/underscore-separated token from that basename
    - the display_name tokens (when set and different from basename)
    - high-frequency vocabulary terms associated with the project

    Returns an empty dict when the DB is unavailable (callers must handle that
    gracefully — the dict is used only for best-effort keyword scoring).
    """
    conn = _open_ro()
    if conn is None:
        return {}
    try:
        projects = _projects_table(conn)
        result: dict[str, list[str]] = {}
        for p in projects:
            cwd = p["cwd"] or ""
            display = (p["display_name"] or "").strip()
            if not cwd:
                continue
            scope = _scope_from_cwd_string(cwd)
            if not scope:
                continue
            kws: list[str] = [scope]
            # tokenize basename
            for tok in re.split(r"[-_\s]+", scope):
                tok = tok.strip()
                if len(tok) >= 3:
                    kws.append(tok)
            # display_name tokens
            if display and display.lower() != scope:
                kws.append(display.lower())
                for tok in re.split(r"[-_\s]+", display.lower()):
                    tok = tok.strip()
                    if len(tok) >= 3:
                        kws.append(tok)
            # vocabulary terms associated with this project
            vocab = _vocab_for_cwd(conn, cwd, limit=8)
            kws.extend(v.lower() for v in vocab if len(v) >= 3)
            result[scope] = list(dict.fromkeys(kws))  # deduplicate, preserve order
        return result
    except Exception:
        return {}
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# Module-level cache — populated lazily on first call to score_scopes().
# Hooks are short-lived processes so one build per process is fine.
_SCOPE_KEYWORDS_CACHE: dict[str, list[str]] | None = None


def _get_scope_keywords() -> dict[str, list[str]]:
    global _SCOPE_KEYWORDS_CACHE
    if _SCOPE_KEYWORDS_CACHE is None:
        _SCOPE_KEYWORDS_CACHE = _build_scope_keywords()
    return _SCOPE_KEYWORDS_CACHE


# Public attribute — kept for API stability; reflects whatever is in the cache
# at import time.  Tests that write to SCOPE_KEYWORDS directly should call
# _get_scope_keywords() for the live value.
SCOPE_KEYWORDS: dict[str, list[str]] = {}  # populated lazily


# ---------------------------------------------------------------------------
# Pure-string fallback: derive scope from path basename
# ---------------------------------------------------------------------------

def _scope_from_cwd_string(cwd: str) -> str | None:
    """Generic scope from the last meaningful path segment.

    Works for any installer — no hardcoded paths.
    Examples:
        /home/dana/nova-api          -> "nova-api"
        /home/operator/ip-service-for-docker -> "ip-service-for-docker"
        /                            -> None
    """
    if not cwd:
        return None
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    if not parts:
        return None
    return parts[-1].lower()


# ---------------------------------------------------------------------------
# Public: infer_scope
# ---------------------------------------------------------------------------

def infer_scope(cwd: str) -> str | None:
    """Map a cwd to a scope name.

    Priority:
    1. Exact match in ``projects`` table (cwd == stored cwd).
    2. Basename match in ``projects`` table.
    3. Pure-string fallback: basename of the cwd argument.

    Never raises; returns None only when cwd is empty/root with no usable basename.
    """
    if not cwd:
        return None

    conn = _open_ro()
    if conn is not None:
        try:
            projects = _projects_table(conn)
        except Exception:
            projects = []
        finally:
            with contextlib.suppress(Exception):
                conn.close()

        # Exact cwd match
        for p in projects:
            if p["cwd"] == cwd:
                # Prefer display_name if set, else basename of stored cwd
                display = (p["display_name"] or "").strip()
                return display.lower() if display else _scope_from_cwd_string(p["cwd"])

        # Basename match (handles cases where the same project lives under
        # different home dirs on different machines)
        cwd_base = _scope_from_cwd_string(cwd)
        for p in projects:
            if _scope_from_cwd_string(p["cwd"]) == cwd_base:
                display = (p["display_name"] or "").strip()
                return display.lower() if display else cwd_base

    # Fallback: derive from the cwd string alone
    return _scope_from_cwd_string(cwd)


# ---------------------------------------------------------------------------
# Public: score_scopes, dominant_scope
# ---------------------------------------------------------------------------

PIVOT_REGEX = re.compile(
    r"\b(now|instead|switch(?:ing)?|pivot|actually|let'?s\s+(?:do|work|check|look)|"
    r"back\s+to|move\s+(?:on\s+)?to|forget\s+that|new\s+topic)\b",
    re.IGNORECASE,
)


@dataclass
class ScopeShift:
    reason: str                  # "cwd" | "keyword" | "keyword+pivot"
    new: str                     # scope name
    old: str | None
    confidence: float            # 0..1


def score_scopes(prompt: str) -> dict[str, int]:
    """Count keyword hits per scope using dynamically-built keyword table.

    When the DB is empty or unavailable the keyword table is empty and this
    returns an empty dict — callers treat that as "no scope signal from text".
    """
    kws = _get_scope_keywords()
    # Keep module-level SCOPE_KEYWORDS in sync for any code that reads it directly.
    global SCOPE_KEYWORDS
    SCOPE_KEYWORDS = kws

    pl = prompt.lower()
    return {scope: sum(1 for k in kw_list if k in pl)
            for scope, kw_list in kws.items()}


def dominant_scope(prompts: list[str]) -> str | None:
    """Majority-vote scope across N recent prompts."""
    if not prompts:
        return None
    counts: dict[str, int] = {}
    for p in prompts:
        scores = score_scopes(p)
        if scores:
            top = max(scores.items(), key=lambda x: x[1])
            if top[1] > 0:
                counts[top[0]] = counts.get(top[0], 0) + 1
    return max(counts.items(), key=lambda x: x[1])[0] if counts else None


def detect_scope_shift(
    current_prompt: str,
    recent_prompts: list[str],   # last 5 user prompts (excluding current)
    current_cwd: str,
    last_cwd: str | None,
) -> ScopeShift | None:
    """Return a ScopeShift only when confidence is high."""
    # Strong signal: cwd changed
    if last_cwd is not None and current_cwd != last_cwd:
        new = infer_scope(current_cwd) or current_cwd
        old = infer_scope(last_cwd) if last_cwd else None
        return ScopeShift(reason="cwd", new=new, old=old, confidence=1.0)

    # Keyword vote
    scores = score_scopes(current_prompt)
    if not scores or max(scores.values(), default=0) == 0:
        return None
    top_scope, top_count = max(scores.items(), key=lambda x: x[1])
    recent_scope = dominant_scope(recent_prompts[-5:])
    if top_scope == recent_scope:
        return None  # same scope, no shift

    pivot = bool(PIVOT_REGEX.search(current_prompt))
    confidence = 0.6 + (0.3 if pivot else 0.0) + min(0.1 * top_count, 0.2)
    if confidence < 0.7:
        return None
    return ScopeShift(
        reason=("keyword+pivot" if pivot else "keyword"),
        new=top_scope,
        old=recent_scope,
        confidence=min(confidence, 1.0),
    )
