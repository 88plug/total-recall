"""SQL aggregation helpers powering ``total-recall metrics``.

Every public function in this module takes a path to the SQLite index and
returns a plain dict / list-of-dicts shaped for direct JSON-ification by
the CLI layer. The module is deliberately *defensive*:

* The v2 tables (``turns``, ``compactions``, ``ingest_runs``) may be
  missing on a partially-migrated DB. Every query that touches them is
  wrapped in a ``try/except sqlite3.OperationalError`` and falls back to
  ``0`` / ``[]`` / ``{}`` so the CLI still produces useful output instead
  of crashing.
* The cost-estimation rates live in ``total_recall.cost`` (sibling
  worktree MA6). If that module hasn't landed yet we use a small inline
  default so we still degrade gracefully.

Time windows are expressed as a `since` string: ``'7d'``, ``'24h'``,
``'30m'`` (relative durations from "now") or ``'YYYY-MM-DD'`` (absolute
UTC midnight). Empty / ``None`` means "all time" (epoch 0).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "summary",
    "cost",
    "sessions",
    "topics",
    "health",
    "DEFAULT_RATES",
]


# ---------------------------------------------------------------------------
# Cost rates — prefer the canonical source from MA6 when present.
# ---------------------------------------------------------------------------

try:
    from total_recall.cost import DEFAULT_RATES as _MA6_RATES  # type: ignore
    from total_recall.cost import estimate as _ma6_estimate  # type: ignore

    DEFAULT_RATES: dict[str, tuple[float, float]] = dict(_MA6_RATES)
    _HAS_MA6 = True
except Exception:  # pragma: no cover - exercised when MA6 absent
    DEFAULT_RATES = {
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-opus-4-7": (15.0, 75.0),
        "default": (3.0, 15.0),
    }
    _HAS_MA6 = False

    def _ma6_estimate(  # type: ignore[no-redef]
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        rates: dict[str, tuple[float, float]] | None = None,
    ) -> float:
        """Tiny inline cost estimator used when MA6 hasn't merged yet."""
        table = rates or DEFAULT_RATES
        rate_in, rate_out = table.get(model, table.get("default", (3.0, 15.0)))
        # Cache reads bill at ~10% of input (Anthropic's documented rate),
        # cache writes bill at ~125%. Good enough for an estimate.
        in_cost = (input_tokens / 1_000_000.0) * rate_in
        cache_read_cost = (cache_read_tokens / 1_000_000.0) * rate_in * 0.10
        cache_create_cost = (cache_creation_tokens / 1_000_000.0) * rate_in * 1.25
        out_cost = (output_tokens / 1_000_000.0) * rate_out
        return float(in_cost + cache_read_cost + cache_create_cost + out_cost)


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdwSMHDW])\s*$")
_ISO_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*$")


# ---------------------------------------------------------------------------
# Correction normalization
# ---------------------------------------------------------------------------
#
# Real corpus shows the same standing rule paraphrased many ways:
#   "use Edit not Write"
#   "use Edit instead of Write"
#   "stop using Write, use Edit"
# Exact-match groupby in SQL treats each as count=1, which buries
# recurring rules in the top_corrections / topics outputs. We collapse
# them to a normalized key (lowercased, trigger-words stripped, fillers
# dropped, first 5 tokens) and bucket in Python.

_TRIGGER_RE = re.compile(
    r"^(?:no[,!]?\s+|stop\s+|wait[,!]?\s+|don'?t\s+|actually[,!]?\s+|"
    r"nope[,!]?\s+|i said\s+|i told you\s+|fuck(?:ing)?\s+|wrong[,!]?\s+|"
    r"never\s+)+",
    re.IGNORECASE,
)

# Triggers that carry "not" semantics ("no", "stop", "don't", "never"):
# strip them but inject a "not" token so paraphrases like
# "use Edit not Write" and "stop using Write, use Edit" bucket together.
_NEGATING_TRIGGER_RE = re.compile(
    r"^(?:no[,!]?\s+|stop\s+|don'?t\s+|nope[,!]?\s+|never\s+)+",
    re.IGNORECASE,
)

_FILLER_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "you",
        "we",
        "that",
        "this",
        "it",
        "to",
        "do",
        "did",
        "doing",
        "just",
        "please",
        "here",
    }
)

# Tiny alias table for inflections we see often in correction text.
# Keeps "use" and "using" in the same bucket without dragging in a stemmer.
_TOKEN_ALIASES: dict[str, str] = {
    "using": "use",
    "uses": "use",
    "used": "use",
    "writes": "write",
    "writing": "write",
    "edits": "edit",
    "editing": "edit",
}


def _normalize_correction(text: str) -> str:
    """Collapse paraphrased corrections to a stable grouping key.

    - lowercase, trim
    - strip leading trigger words ("no,", "stop", "wait", "don't", ...)
    - drop common filler tokens (a, the, you, to, ...)
    - normalize a few inflections (gerunds → bare verb, "instead of" → "not")
    - keep the first 5 surviving tokens, sorted, so word-order variants
      of the same rule ("use Edit not Write" / "stop using Write, use
      Edit") bucket together
    - if everything got stripped (< 3 chars), fall back to the first 60
      chars of the lowercased original (never raises, never returns "")
    """
    if not text:
        return ""
    lowered = text.lower().strip()
    has_negating_trigger = bool(_NEGATING_TRIGGER_RE.match(lowered))
    stripped = _TRIGGER_RE.sub("", lowered)
    # "instead of" carries the same semantics as "not" in correction text.
    stripped = re.sub(r"\binstead\s+of\b", "not", stripped)
    if has_negating_trigger and " not " not in f" {stripped} ":
        # Carry forward the "not" implied by the stripped trigger so
        # "stop X, use Y" buckets with "use Y not X".
        stripped = stripped + " not"
    # Collapse non-alphanum to single spaces so punctuation doesn't keep
    # otherwise-identical phrasings apart.
    tokenized = re.sub(r"[^a-z0-9']+", " ", stripped).strip()
    tokens: list[str] = []
    for t in tokenized.split():
        if not t or t in _FILLER_WORDS:
            continue
        t = _TOKEN_ALIASES.get(t, t)
        tokens.append(t)
    # Sort to make order-insensitive: "use edit not write" and
    # "use write use edit" both reduce to the same key once de-duped.
    key = " ".join(sorted(set(tokens))[:5])
    if len(key) < 3:
        return lowered[:60]
    return key


_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 7 * 86_400,
}


def _parse_since(since: str | None) -> int:
    """Convert a duration / ISO-date string to a unix-epoch lower bound.

    Returns 0 for empty / ``None`` ("all time").
    """
    if since is None:
        return 0
    s = str(since).strip()
    if not s:
        return 0

    dur = _DURATION_RE.match(s)
    if dur:
        n = int(dur.group(1))
        unit = dur.group(2).lower()
        delta = n * _UNIT_SECONDS[unit]
        now = int(datetime.now(tz=timezone.utc).timestamp())
        return max(0, now - delta)

    iso = _ISO_DATE_RE.match(s)
    if iso:
        dt = datetime.strptime(iso.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    # Unknown format: be lenient — treat as 'all time' rather than raising.
    return 0


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path) -> sqlite3.Connection:
    """Open the index DB read-only-ish.

    We use a regular (read-write) connection so the caller can pass a
    fresh path; the schema bootstrap in ``index.db.connect`` is *not*
    invoked here — these are pure SELECTs, and we'd rather get a clean
    ``OperationalError`` for a missing table than a side-effecting
    schema apply.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    """Run a SELECT-scalar; return None when the underlying table is gone."""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return row[0]


def _safe_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params).fetchall())
    except sqlite3.OperationalError:
        return []


def _project_filter(project: str | None, alias: str = "") -> tuple[str, list[Any]]:
    """Build a WHERE-clause fragment for an optional project (cwd) filter."""
    if not project:
        return "", []
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}cwd = ?", [project]


# ---------------------------------------------------------------------------
# summary()
# ---------------------------------------------------------------------------


def summary(
    db_path: Path,
    since: str | None = "7d",
    project: str | None = None,
) -> dict:
    """High-level aggregate metrics over the configured time window.

    See the module docstring for the returned shape.
    """
    since_ts = _parse_since(since)
    conn = _open(db_path)
    try:
        proj_clause_m, proj_params_m = _project_filter(project, "m")
        proj_clause_t, proj_params_t = _project_filter(project, "t")
        proj_clause_c, proj_params_c = _project_filter(project, "c")

        # Session / project counts come from `messages` (always present).
        # Strip per-agent worktree subdirs from cwd so subagent runs don't
        # inflate the project count (each /<repo>/.claude/worktrees/agent-X is
        # its own cwd in the raw data — we want to roll those up to <repo>).
        sess_row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.session_id) AS n_sessions,
                   COUNT(DISTINCT
                     CASE
                       WHEN instr(m.cwd, '/.claude/worktrees/') > 0
                         THEN substr(m.cwd, 1, instr(m.cwd, '/.claude/worktrees/') - 1)
                       ELSE m.cwd
                     END
                   ) AS n_projects
              FROM messages m
             WHERE COALESCE(m.ts, 0) >= ?
               {proj_clause_m}
            """,
            (since_ts, *proj_params_m),
        ).fetchone()
        n_sessions = int(sess_row["n_sessions"] or 0) if sess_row else 0
        n_projects = int(sess_row["n_projects"] or 0) if sess_row else 0

        # Token totals + active duration come from `turns` (v2; defensive).
        token_row = None
        if _table_exists(conn, "turns"):
            try:
                token_row = conn.execute(
                    f"""
                    SELECT COALESCE(SUM(t.input_tokens), 0)          AS input_tokens,
                           COALESCE(SUM(t.cache_read_tokens), 0)     AS cache_read_tokens,
                           COALESCE(SUM(t.cache_creation_tokens), 0) AS cache_creation_tokens,
                           COALESCE(SUM(t.output_tokens), 0)         AS output_tokens,
                           COALESCE(SUM(t.duration_ms), 0)           AS active_ms
                      FROM turns t
                     WHERE COALESCE(t.ts, 0) >= ?
                       {proj_clause_t}
                    """,
                    (since_ts, *proj_params_t),
                ).fetchone()
            except sqlite3.OperationalError:
                token_row = None

        input_tokens = int(token_row["input_tokens"]) if token_row else 0
        cache_read_tokens = int(token_row["cache_read_tokens"]) if token_row else 0
        cache_creation_tokens = int(token_row["cache_creation_tokens"]) if token_row else 0
        output_tokens = int(token_row["output_tokens"]) if token_row else 0
        active_ms = int(token_row["active_ms"]) if token_row else 0

        # cache_read_pct: cache_read / (input + cache_read + cache_creation)
        denom = input_tokens + cache_read_tokens + cache_creation_tokens
        cache_read_pct = (100.0 * cache_read_tokens / denom) if denom > 0 else 0.0

        # Compactions (v2; defensive).
        n_compactions = 0
        if _table_exists(conn, "compactions"):
            n_compactions = int(
                _safe_scalar(
                    conn,
                    f"""
                    SELECT COUNT(*) FROM compactions c
                     WHERE COALESCE(c.ts, 0) >= ?
                       {proj_clause_c}
                    """,
                    (since_ts, *proj_params_c),
                )
                or 0
            )

        # Estimated cost: sum per-model from `turns`.
        estimated_cost = 0.0
        if _table_exists(conn, "turns"):
            try:
                rows = conn.execute(
                    f"""
                    SELECT t.model,
                           COALESCE(SUM(t.input_tokens), 0)          AS input_tokens,
                           COALESCE(SUM(t.cache_read_tokens), 0)     AS cache_read_tokens,
                           COALESCE(SUM(t.cache_creation_tokens), 0) AS cache_creation_tokens,
                           COALESCE(SUM(t.output_tokens), 0)         AS output_tokens
                      FROM turns t
                     WHERE COALESCE(t.ts, 0) >= ?
                       {proj_clause_t}
                     GROUP BY t.model
                    """,
                    (since_ts, *proj_params_t),
                ).fetchall()
                for r in rows:
                    estimated_cost += _ma6_estimate(
                        model=r["model"] or "default",
                        input_tokens=int(r["input_tokens"] or 0),
                        output_tokens=int(r["output_tokens"] or 0),
                        cache_read_tokens=int(r["cache_read_tokens"] or 0),
                        cache_creation_tokens=int(r["cache_creation_tokens"] or 0),
                    )
            except sqlite3.OperationalError:
                pass

        # Wall hours: per-session (max - min), then summed in the outer
        # query — we can't nest aggregates directly in SQLite.
        wall_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(span), 0) AS wall_secs FROM (
              SELECT (MAX(m.ts) - MIN(m.ts)) AS span
                FROM messages m
               WHERE COALESCE(m.ts, 0) >= ?
                 {proj_clause_m}
               GROUP BY m.session_id
            )
            """,
            (since_ts, *proj_params_m),
        ).fetchone()
        wall_secs = int(wall_row["wall_secs"] or 0) if wall_row else 0

        # Top corrections: pull rows from `extractions` (kind='correction')
        # and bucket in Python by a normalized key so paraphrased variants
        # of the same standing rule count together instead of each scoring 1.
        top_corrections: list[dict[str, Any]] = []
        try:
            ext_clause, ext_params = _project_filter(project, "e")
            cur = conn.execute(
                f"""
                SELECT e.id AS id, e.content AS content, e.ts AS ts
                  FROM extractions e
                 WHERE e.kind = 'correction'
                   AND COALESCE(e.ts, 0) >= ?
                   {ext_clause}
                """,
                (since_ts, *ext_params),
            )
            corr_buckets: dict[str, dict[str, Any]] = {}
            for r in cur.fetchall():
                content = r["content"] or ""
                key = _normalize_correction(content)
                if not key:
                    continue
                b = corr_buckets.setdefault(
                    key,
                    {
                        "normalized": key,
                        "count": 0,
                        "example": content,
                        "last_ts": int(r["ts"] or 0),
                    },
                )
                b["count"] += 1
                # Use the longest content in the group as the example
                # (more context for the human reader).
                if len(content) > len(b["example"]):
                    b["example"] = content
                ts_v = int(r["ts"] or 0)
                if ts_v > b["last_ts"]:
                    b["last_ts"] = ts_v
            ranked = sorted(
                corr_buckets.values(),
                key=lambda d: (d["count"], d["last_ts"]),
                reverse=True,
            )
            top_corrections = [
                {
                    "content": b["example"],
                    "count": int(b["count"]),
                    "normalized": b["normalized"],
                }
                for b in ranked[:3]
            ]
        except sqlite3.OperationalError:
            top_corrections = []

        # Busiest project: by token total (falls back to message count when
        # turns is absent).
        busiest_project: dict[str, Any] = {"cwd": None, "tokens": 0, "sessions": 0}
        if _table_exists(conn, "turns"):
            try:
                row = conn.execute(
                    f"""
                    SELECT t.cwd AS cwd,
                           COALESCE(SUM(
                             t.input_tokens + t.cache_read_tokens +
                             t.cache_creation_tokens + t.output_tokens
                           ), 0) AS tokens,
                           COUNT(DISTINCT t.session_id) AS sessions
                      FROM turns t
                     WHERE COALESCE(t.ts, 0) >= ?
                       {proj_clause_t}
                       AND t.cwd IS NOT NULL
                     GROUP BY t.cwd
                     ORDER BY tokens DESC
                     LIMIT 1
                    """,
                    (since_ts, *proj_params_t),
                ).fetchone()
                if row:
                    busiest_project = {
                        "cwd": row["cwd"],
                        "tokens": int(row["tokens"] or 0),
                        "sessions": int(row["sessions"] or 0),
                    }
            except sqlite3.OperationalError:
                pass
        if busiest_project["cwd"] is None:
            row = conn.execute(
                f"""
                SELECT m.cwd AS cwd,
                       COUNT(*) AS message_count,
                       COUNT(DISTINCT m.session_id) AS sessions
                  FROM messages m
                 WHERE COALESCE(m.ts, 0) >= ?
                   AND m.cwd IS NOT NULL
                   {proj_clause_m}
                 GROUP BY m.cwd
                 ORDER BY message_count DESC
                 LIMIT 1
                """,
                (since_ts, *proj_params_m),
            ).fetchone()
            if row:
                busiest_project = {
                    "cwd": row["cwd"],
                    "tokens": 0,
                    "sessions": int(row["sessions"] or 0),
                }

        # Longest session: rank by total tokens when turns are available,
        # otherwise by wall-clock duration.
        longest_session: dict[str, Any] = {
            "session_id": None,
            "ai_title": None,
            "duration_min": 0.0,
            "tokens": 0,
            "cwd": None,
        }
        long_row = None
        if _table_exists(conn, "turns"):
            try:
                long_row = conn.execute(
                    f"""
                    SELECT t.session_id AS session_id,
                           t.cwd        AS cwd,
                           COALESCE(SUM(
                             t.input_tokens + t.cache_read_tokens +
                             t.cache_creation_tokens + t.output_tokens
                           ), 0) AS tokens,
                           COALESCE(SUM(t.duration_ms), 0) AS dur_ms
                      FROM turns t
                     WHERE COALESCE(t.ts, 0) >= ?
                       {proj_clause_t}
                     GROUP BY t.session_id
                     ORDER BY tokens DESC
                     LIMIT 1
                    """,
                    (since_ts, *proj_params_t),
                ).fetchone()
            except sqlite3.OperationalError:
                long_row = None
        if long_row is None:
            long_row = conn.execute(
                f"""
                SELECT m.session_id AS session_id,
                       (SELECT cwd FROM messages m2
                         WHERE m2.session_id = m.session_id AND m2.cwd IS NOT NULL
                         ORDER BY m2.ts DESC LIMIT 1) AS cwd,
                       0 AS tokens,
                       (MAX(m.ts) - MIN(m.ts)) * 1000 AS dur_ms
                  FROM messages m
                 WHERE COALESCE(m.ts, 0) >= ?
                   {proj_clause_m}
                 GROUP BY m.session_id
                 ORDER BY dur_ms DESC
                 LIMIT 1
                """,
                (since_ts, *proj_params_m),
            ).fetchone()
        if long_row:
            sid = long_row["session_id"]
            title_row = conn.execute(
                """
                SELECT text FROM messages
                 WHERE session_id = ? AND kind = 'ai-title'
                 ORDER BY ts DESC LIMIT 1
                """,
                (sid,),
            ).fetchone()
            longest_session = {
                "session_id": sid,
                "ai_title": title_row["text"] if title_row else None,
                "duration_min": round(float(long_row["dur_ms"] or 0) / 60_000.0, 2),
                "tokens": int(long_row["tokens"] or 0),
                "cwd": long_row["cwd"],
            }

        return {
            "sessions": n_sessions,
            "projects": n_projects,
            "compactions": n_compactions,
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_read_pct": round(cache_read_pct, 2),
            "output_tokens": output_tokens,
            "estimated_cost": round(estimated_cost, 4),
            "active_hours": round(active_ms / 3_600_000.0, 3),
            "wall_hours": round(wall_secs / 3600.0, 3),
            "top_corrections": top_corrections,
            "busiest_project": busiest_project,
            "longest_session": longest_session,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cost()
# ---------------------------------------------------------------------------


def cost(
    db_path: Path,
    rates: dict[str, tuple[float, float]] | None = None,
    since: str | None = "30d",
    project: str | None = None,
) -> dict:
    """Per-model token + cost breakdown.

    ``rates`` overrides the per-model ($/Mtok-in, $/Mtok-out) table. When
    None, uses MA6's ``DEFAULT_RATES`` (or the inline fallback).
    """
    since_ts = _parse_since(since)
    rate_table = rates or DEFAULT_RATES
    conn = _open(db_path)
    try:
        if not _table_exists(conn, "turns"):
            return {"by_model": [], "total_cost": 0.0}
        proj_clause, proj_params = _project_filter(project, "t")
        try:
            rows = conn.execute(
                f"""
                SELECT t.model,
                       COALESCE(SUM(t.input_tokens), 0)          AS input,
                       COALESCE(SUM(t.cache_read_tokens), 0)     AS cache_read,
                       COALESCE(SUM(t.cache_creation_tokens), 0) AS cache_creation,
                       COALESCE(SUM(t.output_tokens), 0)         AS output
                  FROM turns t
                 WHERE COALESCE(t.ts, 0) >= ?
                   {proj_clause}
                 GROUP BY t.model
                 ORDER BY (
                   COALESCE(SUM(t.input_tokens), 0) +
                   COALESCE(SUM(t.output_tokens), 0)
                 ) DESC
                """,
                (since_ts, *proj_params),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"by_model": [], "total_cost": 0.0}

        by_model: list[dict[str, Any]] = []
        total = 0.0
        for r in rows:
            model = r["model"] or "default"
            c_in = int(r["input"] or 0)
            c_cr = int(r["cache_read"] or 0)
            c_cw = int(r["cache_creation"] or 0)
            c_out = int(r["output"] or 0)
            c_cost = _ma6_estimate(
                model=model,
                input_tokens=c_in,
                output_tokens=c_out,
                cache_read_tokens=c_cr,
                cache_creation_tokens=c_cw,
                rates=rate_table,
            )
            total += c_cost
            by_model.append(
                {
                    "model": model,
                    "input": c_in,
                    "cache_read": c_cr,
                    "cache_creation": c_cw,
                    "output": c_out,
                    "cost": round(c_cost, 4),
                }
            )
        return {"by_model": by_model, "total_cost": round(total, 4)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sessions()
# ---------------------------------------------------------------------------


def sessions(
    db_path: Path,
    top: int = 10,
    by: Literal["tokens", "duration", "corrections"] = "tokens",
    since: str | None = "30d",
    project: str | None = None,
) -> list[dict]:
    """Top-N sessions ranked by the given column.

    ``by='tokens'``  → sum(input + cache_read + cache_creation + output)
    ``by='duration'`` → wall-clock (max ts - min ts) per session
    ``by='corrections'`` → count of correction extractions per session
    """
    since_ts = _parse_since(since)
    conn = _open(db_path)
    try:
        proj_clause_m, proj_params_m = _project_filter(project, "m")
        # Baseline: per-session message-derived metadata. Always available.
        meta_rows = conn.execute(
            f"""
            SELECT m.session_id      AS session_id,
                   MIN(m.ts)         AS ts_start,
                   MAX(m.ts)         AS ts_end,
                   (SELECT cwd FROM messages m2
                     WHERE m2.session_id = m.session_id AND m2.cwd IS NOT NULL
                     ORDER BY m2.ts DESC LIMIT 1) AS cwd,
                   (SELECT text FROM messages m3
                     WHERE m3.session_id = m.session_id AND m3.kind = 'ai-title'
                     ORDER BY m3.ts DESC LIMIT 1) AS ai_title
              FROM messages m
             WHERE COALESCE(m.ts, 0) >= ?
               {proj_clause_m}
             GROUP BY m.session_id
            """,
            (since_ts, *proj_params_m),
        ).fetchall()

        per_session: dict[str, dict[str, Any]] = {}
        for r in meta_rows:
            sid = r["session_id"]
            ts_start = int(r["ts_start"] or 0)
            ts_end = int(r["ts_end"] or 0)
            per_session[sid] = {
                "session_id": sid,
                "ai_title": r["ai_title"],
                "cwd": r["cwd"],
                "ts_start": ts_start,
                "ts_end": ts_end,
                "tokens": 0,
                "duration_min": round(max(0, ts_end - ts_start) / 60.0, 2),
                "corrections": 0,
            }

        # Token totals from `turns` when present.
        if _table_exists(conn, "turns"):
            proj_clause_t, proj_params_t = _project_filter(project, "t")
            try:
                tok_rows = conn.execute(
                    f"""
                    SELECT t.session_id AS session_id,
                           COALESCE(SUM(
                             t.input_tokens + t.cache_read_tokens +
                             t.cache_creation_tokens + t.output_tokens
                           ), 0) AS tokens
                      FROM turns t
                     WHERE COALESCE(t.ts, 0) >= ?
                       {proj_clause_t}
                     GROUP BY t.session_id
                    """,
                    (since_ts, *proj_params_t),
                ).fetchall()
                for r in tok_rows:
                    sid = r["session_id"]
                    if sid in per_session:
                        per_session[sid]["tokens"] = int(r["tokens"] or 0)
                    else:
                        per_session[sid] = {
                            "session_id": sid,
                            "ai_title": None,
                            "cwd": None,
                            "ts_start": 0,
                            "ts_end": 0,
                            "tokens": int(r["tokens"] or 0),
                            "duration_min": 0.0,
                            "corrections": 0,
                        }
            except sqlite3.OperationalError:
                pass

        # Correction counts from `extractions`.
        try:
            ext_clause, ext_params = _project_filter(project, "e")
            corr_rows = conn.execute(
                f"""
                SELECT e.session_id, COUNT(*) AS n
                  FROM extractions e
                 WHERE e.kind = 'correction'
                   AND COALESCE(e.ts, 0) >= ?
                   {ext_clause}
                 GROUP BY e.session_id
                """,
                (since_ts, *ext_params),
            ).fetchall()
            for r in corr_rows:
                sid = r["session_id"]
                if sid in per_session:
                    per_session[sid]["corrections"] = int(r["n"] or 0)
        except sqlite3.OperationalError:
            pass

        rank_key = {
            "tokens": lambda d: d["tokens"],
            "duration": lambda d: d["duration_min"],
            "corrections": lambda d: d["corrections"],
        }.get(by, lambda d: d["tokens"])

        out = sorted(per_session.values(), key=rank_key, reverse=True)
        return out[: max(0, int(top))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# topics()
# ---------------------------------------------------------------------------


def topics(
    db_path: Path,
    since: str | None = "30d",
    limit: int = 10,
    project: str | None = None,
) -> list[dict]:
    """Most-extracted topics over the window.

    A "topic" is the lowercased first ~6-word phrase of an extraction's
    ``content``, which gives us a stable-ish grouping across paraphrased
    corrections/decisions without needing NLP. We bucket on that, count
    rows, surface the distinct extraction kinds that contributed, and
    keep one example content as a human-readable excerpt.
    """
    since_ts = _parse_since(since)
    conn = _open(db_path)
    try:
        proj_clause, proj_params = _project_filter(project, "e")
        try:
            rows = conn.execute(
                f"""
                SELECT e.kind AS kind, e.content AS content
                  FROM extractions e
                 WHERE COALESCE(e.ts, 0) >= ?
                   AND e.content IS NOT NULL
                   {proj_clause}
                """,
                (since_ts, *proj_params),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            content = (r["content"] or "").strip()
            if not content:
                continue
            kind = r["kind"] or ""
            if kind == "correction":
                # Use the shared correction normalizer so paraphrased
                # "use Edit not Write" variants collapse into one topic.
                key = _normalize_correction(content)
            else:
                # First-noun-phrase heuristic: take first ~6 words,
                # lowercased, with non-alphanum collapsed to single spaces.
                norm = re.sub(r"[^a-z0-9]+", " ", content.lower()).strip()
                words = norm.split()
                if not words:
                    continue
                key = " ".join(words[:6])
            if not key:
                continue
            b = buckets.setdefault(
                key,
                {"topic": key, "count": 0, "kinds": set(), "example_content": content},
            )
            b["count"] += 1
            b["kinds"].add(kind)

        ranked = sorted(buckets.values(), key=lambda d: d["count"], reverse=True)
        out: list[dict] = []
        for b in ranked[: max(0, int(limit))]:
            out.append(
                {
                    "topic": b["topic"],
                    "count": int(b["count"]),
                    "kinds": sorted(k for k in b["kinds"] if k),
                    "example_content": b["example_content"],
                }
            )
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def health(db_path: Path) -> dict:
    """Index health snapshot for the CLI/MCP dashboard."""
    conn = _open(db_path)
    try:
        total_messages = int(_safe_scalar(conn, "SELECT COUNT(*) FROM messages") or 0)
        total_extractions = int(_safe_scalar(conn, "SELECT COUNT(*) FROM extractions") or 0)
        total_turns = int(_safe_scalar(conn, "SELECT COUNT(*) FROM turns") or 0)
        total_compactions = int(_safe_scalar(conn, "SELECT COUNT(*) FROM compactions") or 0)

        last_ingest_ts: int | None = None
        ingest_runs_7d = 0
        p50_ingest_ms = 0
        p95_ingest_ms = 0
        error_count_7d = 0

        if _table_exists(conn, "ingest_runs"):
            row = conn.execute("SELECT MAX(ts) AS last_ts FROM ingest_runs").fetchone()
            last_ingest_ts = int(row["last_ts"]) if row and row["last_ts"] else None

            cutoff = int(datetime.now(tz=timezone.utc).timestamp()) - 7 * 86_400
            cnt_row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(errors), 0) AS errs "
                "FROM ingest_runs WHERE ts >= ?",
                (cutoff,),
            ).fetchone()
            if cnt_row:
                ingest_runs_7d = int(cnt_row["n"] or 0)
                error_count_7d = int(cnt_row["errs"] or 0)

            # Percentiles: pull the elapsed_ms column ordered, do it in Python
            # (cheap; SQLite has no built-in percentile_cont).
            elapsed = [
                int(r["elapsed_ms"] or 0)
                for r in conn.execute(
                    "SELECT elapsed_ms FROM ingest_runs WHERE ts >= ? ORDER BY elapsed_ms",
                    (cutoff,),
                ).fetchall()
            ]
            if elapsed:
                import math

                n = len(elapsed)

                def _pick(pct: float) -> int:
                    # Nearest-rank percentile, clamped to [0, n-1].
                    idx = max(0, min(n - 1, math.ceil(pct * n) - 1))
                    return elapsed[idx]

                p50_ingest_ms = _pick(0.50)
                p95_ingest_ms = _pick(0.95)

        now = int(datetime.now(tz=timezone.utc).timestamp())
        last_age = (now - last_ingest_ts) if last_ingest_ts is not None else None

        try:
            db_size = Path(db_path).stat().st_size
        except OSError:
            db_size = 0

        bootstrap = _bootstrap_status(Path(db_path).parent)

        return {
            "last_ingest_ts": last_ingest_ts,
            "last_ingest_age_seconds": last_age,
            "total_messages": total_messages,
            "total_extractions": total_extractions,
            "total_turns": total_turns,
            "total_compactions": total_compactions,
            "ingest_runs_7d": ingest_runs_7d,
            "p50_ingest_ms": int(p50_ingest_ms),
            "p95_ingest_ms": int(p95_ingest_ms),
            "error_count_7d": error_count_7d,
            "db_size_bytes": int(db_size),
            "bootstrap": bootstrap,
        }
    finally:
        conn.close()


def _bootstrap_status(data_dir: Path) -> dict | None:
    """Read .bootstrapping lockfile if present; return status dict or None.

    Status dict shape:
        {"in_progress": bool, "pid": int|None, "started": str, "hook": str,
         "log_tail": "...last few lines of bootstrap.log..."}
    """
    lock = data_dir / ".bootstrapping"
    if not lock.exists():
        return None
    try:
        line = lock.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        parts = line.split(maxsplit=2)
        pid = int(parts[0]) if parts and parts[0].isdigit() else None
        started = parts[1] if len(parts) > 1 else ""
        hook = parts[2] if len(parts) > 2 else "?"
    except (OSError, IndexError, ValueError):
        return {"in_progress": True, "pid": None, "started": "", "hook": "?"}

    in_progress = False
    if pid is not None:
        try:
            import os

            os.kill(pid, 0)
            in_progress = True
        except (OSError, ProcessLookupError):
            in_progress = False

    tail = ""
    log_path = data_dir / "logs" / "bootstrap.log"
    if log_path.exists():
        try:
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", errors="replace")[-500:]
        except OSError:
            pass

    return {
        "in_progress": in_progress,
        "pid": pid,
        "started": started,
        "hook": hook,
        "log_tail": tail,
    }
