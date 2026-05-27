"""Typed read API for the total-recall index.

All callers (MCP server, slash commands, SessionStart hook) go through this
module. The intent is twofold:

1. **Latency.** Every query here is shaped to use a single index — either an
   FTS5 table or one of the structured indexes on ``messages`` /
   ``extractions``. Target is <100ms for the MCP "recall_*" tools.
2. **Hygiene.** FTS5 MATCH strings have to be quoted carefully (operators,
   special chars). All raw-user-input goes through :func:`_fts_match_quote`
   so a search for ``go-1.23 vs. go1.22`` doesn't blow up the parser.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "QueryHit",
    "search_extractions",
    "search_messages",
    "top_topics_for_cwd",
    "session_count_for_cwd",
    "list_sessions_for_cwd",
    "get_session_meta",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class QueryHit:
    """One result row from :func:`search_extractions`.

    ``score`` combines extractor confidence (the stored ``score`` column)
    with FTS5 BM25 rank when a text ``query`` was supplied. Higher is
    better; the API normalizes everything into ``[0.0, 1.0]`` so callers
    can rank/threshold without thinking about BM25 sign conventions.
    """

    kind: str
    content: str
    session_id: str
    cwd: str
    ts: datetime
    score: float
    context: dict[str, Any] = field(default_factory=dict)
    # Stable rowid of the source ``extractions`` row. Needed by the RRF
    # fusion layer (``vec/rrf.py``) so FTS-vs-vec lanes can dedupe on the
    # same extraction surfacing through both retrievers.
    extraction_id: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# FTS5 reserves a small set of characters as operators. The cheapest way to
# pass an arbitrary user query through MATCH safely is to split on whitespace
# and wrap each non-empty term in double quotes, escaping embedded quotes.
_FTS_SPLIT = re.compile(r"\s+")


def _fts_match_quote(query: str) -> str:
    """Sanitize a free-text query for FTS5 ``MATCH``.

    Each whitespace-delimited term is double-quoted (FTS5 treats a quoted
    phrase as a literal token, defanging operators like ``OR``, ``NEAR``,
    ``-``, ``"``, ``*``, parens, and ``:``). Empty strings yield ``""``.
    """
    if not query:
        return ""
    terms = [t for t in _FTS_SPLIT.split(query.strip()) if t]
    if not terms:
        return ""
    quoted = []
    for t in terms:
        # Escape embedded double quotes per FTS5: "" inside a quoted token.
        safe = t.replace('"', '""')
        quoted.append(f'"{safe}"')
    return " ".join(quoted)


def _to_dt(ts: Any) -> datetime:
    """Cast a unix-epoch int / ISO string back to an aware datetime."""
    if ts is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _decode_context(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _bm25_to_score(rank: float | None, base: float) -> float:
    """Combine FTS5 BM25 rank with the stored extractor confidence.

    FTS5's ``bm25()`` returns a *more-negative-is-better* value. We invert
    + squash via ``1 / (1 + e^rank)``, then average with the stored
    ``score``. Result is clamped to ``[0, 1]``.
    """
    if rank is None:
        return max(0.0, min(1.0, float(base)))
    # exp() can overflow on extreme ranks; guard it.
    try:
        import math

        fts_norm = 1.0 / (1.0 + math.exp(rank))
    except (OverflowError, ValueError):
        fts_norm = 0.0 if rank > 0 else 1.0
    combined = 0.5 * fts_norm + 0.5 * float(base)
    return max(0.0, min(1.0, combined))


# ---------------------------------------------------------------------------
# Public queries
# ---------------------------------------------------------------------------


def search_extractions(
    conn: sqlite3.Connection,
    query: str | None = None,
    cwd: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    since: datetime | None = None,
    limit: int = 10,
) -> list[QueryHit]:
    """Search the ``extractions`` table.

    All filters are AND-ed. When ``query`` is provided the result is ranked
    by BM25; otherwise rows come back ordered by ``ts DESC`` then ``score
    DESC``. The combined score on returned :class:`QueryHit`s normalizes
    BM25 + stored extractor confidence into ``[0, 1]``.
    """
    where: list[str] = []
    params: list[Any] = []

    if cwd is not None:
        where.append("e.cwd = ?")
        params.append(cwd)
    if kind is not None:
        where.append("e.kind = ?")
        params.append(kind)
    if scope is not None:
        where.append("e.scope = ?")
        params.append(scope)
    if since is not None:
        since_int = int(since.timestamp()) if isinstance(since, datetime) else int(since)
        where.append("e.ts >= ?")
        params.append(since_int)

    match = _fts_match_quote(query) if query else ""

    if match:
        sql = """
            SELECT e.id, e.kind, e.content, e.session_id, e.cwd, e.ts,
                   e.score, e.context_json, bm25(extractions_fts) AS rank
              FROM extractions_fts
              JOIN extractions e ON e.id = extractions_fts.rowid
             WHERE extractions_fts MATCH ?
        """
        sql_params: list[Any] = [match]
        if where:
            sql += " AND " + " AND ".join(where)
            sql_params.extend(params)
        sql += " ORDER BY rank LIMIT ?"
        sql_params.append(limit)
    else:
        sql = "SELECT e.id, e.kind, e.content, e.session_id, e.cwd, e.ts, " \
              "e.score, e.context_json, NULL AS rank FROM extractions e"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.ts DESC, e.score DESC LIMIT ?"
        sql_params = list(params) + [limit]

    cur = conn.execute(sql, sql_params)
    hits: list[QueryHit] = []
    for row in cur.fetchall():
        hits.append(
            QueryHit(
                kind=row["kind"],
                content=row["content"],
                session_id=row["session_id"],
                cwd=row["cwd"] or "",
                ts=_to_dt(row["ts"]),
                score=_bm25_to_score(row["rank"], row["score"] or 0.5),
                context=_decode_context(row["context_json"]),
                extraction_id=int(row["id"]) if row["id"] is not None else 0,
            )
        )
    return hits


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    cwd: str | None = None,
    role: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Raw-message FTS5 search.

    Used by the MCP server's `recall_messages` fallback when extractions
    don't cover the user's query. Returns plain dicts (not :class:`QueryHit`)
    because the schema is different and callers tend to need ``parent_uuid``
    / ``message_uuid`` to re-link to the source DAG.
    """
    match = _fts_match_quote(query)
    if not match:
        return []

    sql = """
        SELECT m.id, m.session_id, m.cwd, m.role, m.kind, m.ts,
               m.parent_uuid, m.message_uuid, m.source_file, m.text,
               bm25(messages_fts) AS rank
          FROM messages_fts
          JOIN messages m ON m.id = messages_fts.rowid
         WHERE messages_fts MATCH ?
    """
    params: list[Any] = [match]
    if cwd is not None:
        sql += " AND m.cwd = ?"
        params.append(cwd)
    if role is not None:
        sql += " AND m.role = ?"
        params.append(role)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    cur = conn.execute(sql, params)
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        d = dict(row)
        d["ts"] = _to_dt(d.get("ts"))
        d["score"] = _bm25_to_score(d.pop("rank", None), 0.5)
        out.append(d)
    return out


def top_topics_for_cwd(
    conn: sqlite3.Connection,
    cwd: str,
    limit: int = 5,
) -> list[str]:
    """Return the most recent / highest-confidence extraction contents for a cwd.

    "Topics" here is loose — we just surface the latest distinct extraction
    contents for a project, weighted by score. The MCP server feeds these
    into the SessionStart signpost so the model walks in knowing roughly
    what this project is about.
    """
    cur = conn.execute(
        """
        SELECT content
          FROM extractions
         WHERE cwd = ?
         GROUP BY content
         ORDER BY MAX(score) DESC, MAX(ts) DESC
         LIMIT ?
        """,
        (cwd, limit),
    )
    return [row["content"] for row in cur.fetchall()]


def session_count_for_cwd(conn: sqlite3.Connection, cwd: str) -> int:
    """Count distinct sessions known for a cwd."""
    cur = conn.execute(
        "SELECT COUNT(DISTINCT session_id) AS n FROM messages WHERE cwd = ?",
        (cwd,),
    )
    row = cur.fetchone()
    return int(row["n"]) if row else 0


def list_sessions_for_cwd(
    conn: sqlite3.Connection,
    cwd: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return per-session summaries for a cwd, newest first.

    Each row carries ``session_id``, ``first_ts``, ``last_ts``,
    ``message_count``, and ``ai_title`` (best-effort: the most recent
    ``ai-title`` text we saw for that session, else ``None``).
    """
    cur = conn.execute(
        """
        SELECT m.session_id,
               MIN(m.ts) AS first_ts,
               MAX(m.ts) AS last_ts,
               COUNT(*)  AS message_count,
               (SELECT m2.text
                  FROM messages m2
                 WHERE m2.session_id = m.session_id
                   AND m2.kind = 'ai-title'
                 ORDER BY m2.ts DESC
                 LIMIT 1) AS ai_title
          FROM messages m
         WHERE m.cwd = ?
         GROUP BY m.session_id
         ORDER BY last_ts DESC
         LIMIT ?
        """,
        (cwd, limit),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        out.append(
            {
                "session_id": row["session_id"],
                "first_ts": _to_dt(row["first_ts"]),
                "last_ts": _to_dt(row["last_ts"]),
                "message_count": int(row["message_count"]),
                "ai_title": row["ai_title"],
            }
        )
    return out


def get_session_meta(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Derive per-session metadata from ``messages`` + ``extractions``.

    There is no ``sessions`` table in the schema — session-level facts
    (title, last prompt, time bounds, branch) are reconstructed at read
    time from the message rows. Returns ``None`` if no messages exist for
    ``session_id`` (rather than a half-populated dict, so callers can do a
    cheap truthiness check).

    The shape is stable: ``{session_id, ai_title, last_prompt, cwd,
    started_at, ended_at, message_count, branch, top_extraction_kinds}``.
    """
    row = conn.execute(
        """
        SELECT COUNT(*)  AS message_count,
               MIN(ts)   AS started_at,
               MAX(ts)   AS ended_at,
               -- cwd / branch can vary mid-session in theory; we surface
               -- the most-recent non-null values, which is what the
               -- SessionStart hook actually wants.
               (SELECT cwd FROM messages
                 WHERE session_id = ? AND cwd IS NOT NULL
                 ORDER BY ts DESC LIMIT 1) AS cwd,
               (SELECT git_branch FROM messages
                 WHERE session_id = ? AND git_branch IS NOT NULL
                 ORDER BY ts DESC LIMIT 1) AS branch
          FROM messages
         WHERE session_id = ?
        """,
        (session_id, session_id, session_id),
    ).fetchone()
    if row is None or not row["message_count"]:
        return None

    title_row = conn.execute(
        """
        SELECT text FROM messages
         WHERE session_id = ? AND kind = 'ai-title'
         ORDER BY ts DESC
         LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    last_prompt_row = conn.execute(
        """
        SELECT text FROM messages
         WHERE session_id = ? AND kind = 'last-prompt'
         ORDER BY ts DESC
         LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    kind_rows = conn.execute(
        """
        SELECT kind, COUNT(*) AS n
          FROM extractions
         WHERE session_id = ?
         GROUP BY kind
         ORDER BY n DESC
         LIMIT 5
        """,
        (session_id,),
    ).fetchall()

    return {
        "session_id": session_id,
        "ai_title": title_row["text"] if title_row else None,
        "last_prompt": last_prompt_row["text"] if last_prompt_row else None,
        "cwd": row["cwd"],
        "started_at": _to_dt(row["started_at"]),
        "ended_at": _to_dt(row["ended_at"]),
        "message_count": int(row["message_count"]),
        "branch": row["branch"],
        "top_extraction_kinds": [r["kind"] for r in kind_rows],
    }
