"""MCP tool implementations.

Each tool is a thin wrapper around the SQLite index (WT-4, `index.query`)
and the optional vector layer (WT-5, `vec.rrf.hybrid_search`). When either
sibling module is absent on this branch — typical during parallel worktree
development — the tool degrades to a clear error payload rather than
crashing the MCP server.

Return shape is a plain ``list[dict]`` (or ``dict``) so FastMCP can
auto-serialize it; we keep the keys verbose because they appear directly in
the tool's structured output and are read by both the model and humans
debugging logs.

Sandbox: read-only, no network, no writes to ``~/.claude/projects/``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Literal

from mcp.types import ToolAnnotations

from mcp_server.server import DB_PATH, get_conn, mcp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload bounds — the stdio JSON-RPC wire is the crash surface.
# A single indexed message's raw ``text`` can be a multi-100 KB tool-result
# blob; returning several of them verbatim produced a response large enough to
# break the stdio pipe, which drops the MCP for the WHOLE Claude Code session
# (observed as "total-recall disconnected" mid-conversation). These caps make
# every response bounded regardless of how large the underlying rows are or how
# big a ``limit`` a caller asks for — heavy load can no longer crash the server.
# ---------------------------------------------------------------------------

# Per-field char cap for bulky free-text fields in a returned hit. The message
# tool is documented to return a "snippet", so truncation is the contract, not
# a regression. Short extraction summaries fall well under this and are
# untouched.
_MAX_HIT_TEXT_CHARS = 1500

# Free-text keys that can carry an unbounded blob and must be capped per row.
_TEXT_KEYS = frozenset({"text", "content", "value", "preview", "body", "snippet"})

# Hard ceiling on any tool's ``limit`` — a caller cannot request 10k rows.
_MAX_TOOL_LIMIT = 50

# Backstop cap on total serialized response size (bytes). Even many bounded
# rows are trimmed to stay under this so the wire is never overwhelmed.
_MAX_RESPONSE_BYTES = 256_000


def _clamp_limit(limit: int, default: int = 10) -> int:
    """Coerce a caller-supplied ``limit`` into ``[1, _MAX_TOOL_LIMIT]``."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_TOOL_LIMIT))


def _bound_hit_text(d: dict[str, Any]) -> dict[str, Any]:
    """Truncate bulky free-text fields in a hit dict in place.

    For any oversized text field, keep the first ``_MAX_HIT_TEXT_CHARS`` chars
    and add ``<key>_truncated=True`` + ``<key>_full_chars=<n>`` so the caller
    knows the value was clipped and how much was dropped.
    """
    for key in _TEXT_KEYS:
        v = d.get(key)
        if isinstance(v, str) and len(v) > _MAX_HIT_TEXT_CHARS:
            full = len(v)
            d[key] = v[:_MAX_HIT_TEXT_CHARS]
            d[f"{key}_truncated"] = True
            d[f"{key}_full_chars"] = full
    return d


def _bound_response(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim a hit list so its serialized size stays under ``_MAX_RESPONSE_BYTES``.

    Rows are already per-field-capped by :func:`_row_to_hit`; this is the
    aggregate backstop for the case of many rows. If any rows are dropped, a
    trailing ``_meta`` marker records how many, so truncation is never silent.
    """
    import json

    out: list[dict[str, Any]] = []
    budget = _MAX_RESPONSE_BYTES
    for i, row in enumerate(rows):
        try:
            size = len(json.dumps(row, default=str))
        except Exception:
            size = _MAX_HIT_TEXT_CHARS
        if budget - size < 0 and out:
            dropped = len(rows) - i
            out.append(
                {
                    "_meta": (
                        f"response truncated to stay under "
                        f"{_MAX_RESPONSE_BYTES} bytes; {dropped} more hit(s) omitted"
                    ),
                    "truncated": True,
                    "omitted": dropped,
                }
            )
            break
        budget -= size
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Observability — emit one NDJSON event per MCP tool call so
# `total-recall metrics health` can report MCP traffic alongside hook fires.
# Defensive import: `total_recall.events` may be absent on partial branches.
# ---------------------------------------------------------------------------

try:
    from total_recall.events import emit_event as _emit
except Exception:  # pragma: no cover

    def _emit(*a, **kw):  # type: ignore[no-redef]
        pass


def _traced(event_name: str):
    """
    Wrap a tool with timing + best-effort event emission.      Privacy: only
    numeric/categorical attrs (topic_len, hits, error class)     are forwarded — never raw
    user text. _emit is itself best-effort, but     we add a second try/except so a broken
    event sink can never break the     tool.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            err: str | None = None
            result: Any = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as exc:
                err = type(exc).__name__
                raise
            finally:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                attrs: dict[str, Any] = {}
                topic = kwargs.get("topic")
                if topic is not None:
                    attrs["topic_len"] = len(topic)
                if isinstance(result, list):
                    attrs["hits"] = len(result)
                # pragma: no cover — observability never breaks tools
                with contextlib.suppress(Exception):
                    _emit(event_name, elapsed_ms=elapsed_ms, error=err, **attrs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Defensive imports — WT-4 (index.query) and WT-5 (vec.rrf) may not yet exist
# on this branch. We import lazily inside each tool so server startup never
# fails because of a missing sibling module.
# ---------------------------------------------------------------------------


def _load_query_module():
    """
    Return the WT-4 query module or None with a logged warning.
    """
    try:
        from index import query  # type: ignore
    except Exception as e:  # ImportError or any submodule blow-up
        log.warning("index.query unavailable: %s", e)
        return None
    return query


def _load_vec_module():
    """
    Return the WT-5 vec.rrf module + an Embedder, or (None, None).
    """
    try:
        import vec  # type: ignore
        from vec import rrf  # type: ignore
    except Exception as e:
        log.debug("vec layer unavailable: %s", e)
        return None, None
    try:
        embedder = vec.Embedder()  # type: ignore[attr-defined]
    except Exception as e:
        log.debug("Embedder init failed: %s", e)
        return rrf, None
    return rrf, embedder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_to_iso(value: Any) -> str | None:
    """
    Best-effort timestamp coercion to ISO-8601 string.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _row_to_hit(row: Any) -> dict[str, Any]:
    """
    Coerce a sqlite3.Row / mapping / object into the documented hit dict.
    """
    d: dict[str, Any]
    if hasattr(row, "keys") or isinstance(row, dict):
        d = dict(row)
    else:
        # Dataclass-like object (e.g. QueryHit) — fall back to vars().
        try:
            d = dict(vars(row))
        except TypeError:
            d = {"value": str(row)}
    if "ts" in d:
        d["ts"] = _ts_to_iso(d["ts"])
    if "started_at" in d:
        d["started_at"] = _ts_to_iso(d["started_at"])
    if "last_ts" in d:
        d["last_ts"] = _ts_to_iso(d["last_ts"])
    # Drop bulky fields the model doesn't need by default.
    d.pop("source_uuid_blob", None)
    # Cap any unbounded free-text field so a single huge row (e.g. a giant
    # tool-result message) can't blow up the stdio JSON-RPC response.
    _bound_hit_text(d)
    return d


def _current_cwd() -> str:
    """
    Best-effort: the cwd Claude Code launched in.      Claude Code injects no dedicated env
    var for this, but the child process     inherits the parent cwd. ``$PWD`` is set by most
    shells; we fall back to     :func:`os.getcwd` which reflects the inherited cwd.
    """
    return os.environ.get("PWD") or os.getcwd()


def _index_missing_error() -> list[dict]:
    return [
        {
            "error": "index not initialized; run `total-recall index` to build it",
            "db_path": str(DB_PATH),
        }
    ]


def _scope_meta(scope_used: str, fell_back: bool, requested: str) -> dict:
    """
    Return a leading ``_meta`` row callers prepend to a hit list.      The MCP child often
    starts in a cwd that's not in the index — e.g. the     user runs `claude` from ``~`` or
    a sibling project. Rather than return     silently-empty results, the recall tools retry
    with     ``scope="all_projects"`` and prepend a ``_meta`` entry so the model     knows
    the broader scope was used.
    """
    msg = f"scope_used={scope_used}"
    if fell_back:
        msg = f"0 hits in {requested}; falling back to all_projects"
    return {"_meta": msg, "scope_used": scope_used, "scope_requested": requested}


def _run_with_scope_fallback(
    requested_scope: str,
    runner,  # callable: (cwd_filter: str | None) -> list
):
    """
    Execute ``runner`` honoring smart scope fallback.      ``runner`` is called once with
    the cwd filter implied by     ``requested_scope``. If the result is empty and the caller
    asked for     ``this_cwd``, ``runner`` is invoked again with ``None`` (all projects).
    Returns ``(rows, scope_used, fell_back)``.
    """
    cwd_filter = _current_cwd() if requested_scope == "this_cwd" else None
    rows = runner(cwd_filter)
    if requested_scope == "this_cwd" and not rows:
        rows = runner(None)
        return rows, "all_projects", True
    return rows, requested_scope, False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(title="Recall", annotations=ToolAnnotations(readOnlyHint=True))
@_traced("mcp.recall")
def recall(
    topic: str,
    kind: Literal[
        "any",
        "correction",
        "decision",
        "self_correction",
        "progress",
        "domain_fact",
        "away_summary",
    ] = "any",
    scope: Literal["this_cwd", "all_projects"] = "this_cwd",
    limit: int = 5,
) -> list[dict]:
    """
    Recall prior session memories about a topic across past Claude Code sessions: decisions,
    user corrections, failed approaches, progress, domain facts. Use when the user
    references prior work, asks what they did, or you need to avoid a past mistake. Returns
    ranked hits {content, session_id, cwd, ts, kind, score}. If `scope="this_cwd"` yields 0
    hits the tool automatically retries with `scope="all_projects"` and prepends a `_meta`
    row indicating the broader scope was used — callers should not re-issue the call.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    kind_filter = None if kind == "any" else kind

    # Prefer hybrid (FTS5 + vector RRF) when the optional vec layer is loaded.
    rrf_mod, embedder = _load_vec_module()

    def _run(cwd_filter: str | None):
        if rrf_mod is not None and embedder is not None:
            return rrf_mod.hybrid_search(  # type: ignore[union-attr]
                conn,
                topic,
                embedder,
                limit=limit,
                cwd=cwd_filter,
                kind=kind_filter,
            )
        return query_mod.search_extractions(
            conn,
            query=topic,
            cwd=cwd_filter,
            kind=kind_filter,
            limit=limit,
        )

    try:
        hits, scope_used, fell_back = _run_with_scope_fallback(scope, _run)
    except Exception as e:
        log.exception("recall() failed")
        return [{"error": f"recall failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out: list[dict] = [_row_to_hit(h) for h in hits]
    if fell_back:
        out.insert(0, _scope_meta(scope_used, fell_back, scope))
    return out


@mcp.tool(title="Prior Sessions for CWD", annotations=ToolAnnotations(readOnlyHint=True))
@_traced("mcp.prior_sessions_for_cwd")
def prior_sessions_for_cwd(cwd: str | None = None, limit: int = 10) -> list[dict]:
    """
    List previous Claude Code sessions for this working directory. Returns ai_title,
    last_prompt, started_at, message_count. Use when the user asks 'what have we been
    working on', wants orientation at session start. cwd defaults to current Claude Code
    cwd.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    explicit_cwd = cwd is not None
    target_cwd = cwd if explicit_cwd else _current_cwd()
    try:
        rows = query_mod.list_sessions_for_cwd(conn, cwd=target_cwd, limit=limit)
        fell_back = False
        # Same this_cwd -> all_projects fallback as find_failed_attempts: when
        # the caller didn't pin a cwd and the current one is unknown to the
        # index (e.g. `claude` launched from ~), retry across all projects so
        # orientation queries aren't silently empty.
        if not rows and not explicit_cwd:
            rows = query_mod.list_sessions_for_cwd(conn, cwd=None, limit=limit)
            fell_back = True
    except Exception as e:
        log.exception("prior_sessions_for_cwd() failed")
        return [{"error": f"prior_sessions_for_cwd failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out: list[dict] = [_row_to_hit(r) for r in rows]
    if fell_back:
        out.insert(0, _scope_meta("all_projects", True, "this_cwd"))
    return out


@mcp.tool(title="Find Failed Attempts", annotations=ToolAnnotations(readOnlyHint=True))
@_traced("mcp.find_failed_attempts")
def find_failed_attempts(pattern: str, cwd: str | None = None, limit: int = 5) -> list[dict]:
    """
    Find prior attempts the user rejected — corrections paired with the rejected approach.
    Use before suggesting something to check if already tried and shot down. Returns
    {rejected_approach, correction, session_id, ts}. When `cwd` is omitted the tool searches
    the current cwd first; if that yields 0 hits it automatically retries across all
    projects and prepends a `_meta` row.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    explicit_cwd = cwd is not None
    target_cwd = cwd if explicit_cwd else _current_cwd()

    def _run(cwd_filter: str | None):
        # Hybrid first (same stack as recall) so paraphrase corrections surface.
        rrf_mod, embedder = _load_vec_module()
        if rrf_mod is not None and embedder is not None:
            try:
                return rrf_mod.hybrid_search(
                    conn,
                    pattern,
                    embedder,
                    limit=limit,
                    cwd=cwd_filter,
                    kind="correction",
                )
            except Exception:
                log.debug("find_failed_attempts hybrid failed; FTS fallback", exc_info=True)
        return query_mod.search_extractions(
            conn,
            query=pattern,
            cwd=cwd_filter,
            kind="correction",
            limit=limit,
        )

    try:
        # Filter to corrections; pair each with its rejected_approach from
        # the extraction's context blob (populated by the corrections
        # extractor when a DAG is available).
        hits = _run(target_cwd)
        fell_back = False
        scope_used = "this_cwd" if not explicit_cwd else "explicit_cwd"
        if not hits and not explicit_cwd:
            hits = _run(None)
            fell_back = True
            scope_used = "all_projects"
    except Exception as e:
        log.exception("find_failed_attempts() failed")
        return [{"error": f"find_failed_attempts failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    results: list[dict] = []
    for h in hits:
        d = _row_to_hit(h)
        context = d.get("context") or {}
        if isinstance(context, str):
            # Some schemas store context as JSON text.
            import json

            try:
                context = json.loads(context)
            except (ValueError, TypeError):
                context = {}
        results.append(
            {
                "rejected_approach": context.get("rejected_approach"),
                "correction": d.get("content"),
                "clarification": context.get("clarification"),
                "session_id": d.get("session_id"),
                "cwd": d.get("cwd"),
                "ts": d.get("ts"),
                "score": d.get("score"),
            }
        )
    if fell_back:
        results.insert(0, _scope_meta(scope_used, True, "this_cwd"))
    return results


@mcp.tool(title="Find User Preferences", annotations=ToolAnnotations(readOnlyHint=True))
@_traced("mcp.find_user_preferences")
def find_user_preferences(
    domain: str | None = None,
    scope: Literal["this_cwd", "all_projects"] = "all_projects",
    limit: int = 10,
) -> list[dict]:
    """
    Find recurring user preferences and standing rules extracted across past sessions.
    Examples: provider preferences, formatting preferences, banned tools, repeated
    corrections. Use when picking defaults that might conflict with user habits. Returns
    ranked preference hits {content, session_id, cwd, ts, kind, score}. If
    `scope="this_cwd"` yields 0 hits the tool automatically retries with
    `scope="all_projects"` and prepends a `_meta` row indicating the broader scope was used.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    def _run(cwd_filter: str | None) -> list[Any]:
        # Preferences usually surface as corrections + domain_facts. Free-text
        # domain → hybrid; empty domain → FTS kind browse (hybrid needs a query).
        out: list[Any] = []
        qtext = (domain or "").strip()
        rrf_mod, embedder = _load_vec_module()
        for k in ("correction", "domain_fact"):
            if qtext and rrf_mod is not None and embedder is not None:
                try:
                    chunk = rrf_mod.hybrid_search(
                        conn, qtext, embedder, limit=limit, cwd=cwd_filter, kind=k,
                    )
                    out.extend(chunk)
                    continue
                except Exception:
                    log.debug("find_user_preferences hybrid failed; FTS", exc_info=True)
            chunk = query_mod.search_extractions(
                conn,
                query=domain or "",
                cwd=cwd_filter,
                kind=k,
                limit=limit,
            )
            out.extend(chunk)
        return out

    try:
        hits, scope_used, fell_back = _run_with_scope_fallback(scope, _run)
    except Exception as e:
        log.exception("find_user_preferences() failed")
        return [{"error": f"find_user_preferences failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    # Truncate to the requested limit by score (desc).
    converted = [_row_to_hit(h) for h in hits]
    converted.sort(key=lambda r: r.get("score") or 0.0, reverse=True)
    result = converted[:limit]
    if fell_back:
        result.insert(0, _scope_meta(scope_used, True, scope))
    return result


@mcp.tool(title="Get Session Digest", annotations=ToolAnnotations(readOnlyHint=True))
@_traced("mcp.get_session_digest")
def get_session_digest(session_id: str) -> dict:
    """
    Return a structured digest of one past session: ai_title, last_prompt, top decisions,
    top corrections, progress markers. Use when user references a session by id.
    """
    conn = get_conn()
    if conn is None:
        err = _index_missing_error()[0]
        return err

    query_mod = _load_query_module()
    if query_mod is None:
        return {"error": "index.query module not available on this branch"}

    try:
        digest: dict[str, Any] = {"session_id": session_id}
        # Session metadata lookup. WT-4 (F5) exposes `get_session_meta(conn,
        # session_id)` returning a dict-like row with ai_title, last_prompt,
        # started_at, message_count, cwd. Import defensively because the
        # symbol may not have landed on this branch yet.
        meta: Any = None
        try:
            from index.query import get_session_meta  # type: ignore
        except Exception:
            get_session_meta = None  # type: ignore[assignment]

        if get_session_meta is not None:
            try:
                meta = get_session_meta(conn, session_id)
            except Exception:
                log.debug("get_session_meta raised; falling back", exc_info=True)
                meta = None
        elif hasattr(query_mod, "get_session_meta"):
            # Older alias path — same symbol exposed off the module object.
            try:
                meta = query_mod.get_session_meta(conn, session_id)
            except Exception:
                meta = None

        if meta is None:
            # F5 hasn't landed: try the legacy `sessions` table (older
            # schemas + some test fixtures still populate it) and finally
            # fall back to a messages aggregate so cwd / message_count /
            # started_at are still populated even without a sessions table.
            try:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None:
                meta = _row_to_hit(row)

        if meta is None:
            try:
                row = conn.execute(
                    "SELECT cwd, MIN(ts) AS started_at, COUNT(*) AS message_count "
                    "  FROM messages WHERE session_id = ? GROUP BY session_id",
                    (session_id,),
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row and row["cwd"]:
                fallback: dict[str, Any] = {
                    "cwd": row["cwd"],
                    "started_at": row["started_at"],
                    "message_count": int(row["message_count"] or 0),
                    "ai_title": None,
                    "last_prompt": None,
                }
                # Best-effort ai_title + last_prompt straight from messages.
                try:
                    title_row = conn.execute(
                        "SELECT text FROM messages WHERE session_id = ? "
                        "  AND kind = 'ai-title' ORDER BY ts DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    if title_row:
                        fallback["ai_title"] = title_row["text"]
                except sqlite3.Error:
                    pass
                try:
                    prompt_row = conn.execute(
                        "SELECT text FROM messages WHERE session_id = ? "
                        "  AND role = 'user' ORDER BY ts DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    if prompt_row:
                        fallback["last_prompt"] = prompt_row["text"]
                except sqlite3.Error:
                    pass
                meta = fallback

        if meta is None:
            return {"error": f"session {session_id} not in index"}

        # Normalize timestamp keys via _row_to_hit semantics.
        meta_dict = _row_to_hit(meta) if not isinstance(meta, dict) else dict(meta)
        if "started_at" in meta_dict:
            meta_dict["started_at"] = _ts_to_iso(meta_dict["started_at"])
        for key in ("ai_title", "last_prompt", "started_at", "message_count", "cwd"):
            if key in meta_dict:
                digest[key] = meta_dict[key]

        for kind, label in (
            ("decision", "top_decisions"),
            ("correction", "top_corrections"),
            ("progress", "progress_markers"),
        ):
            try:
                rows = query_mod.search_extractions(
                    conn,
                    query="",
                    kind=kind,
                    limit=50,
                )

                # Session-scope filter. sqlite3.Row and dict both support
                # key lookup but NOT getattr(row, "session_id") — that always
                # returns None, which previously kept every row.
                def _sid(r: Any) -> str | None:
                    if isinstance(r, dict):
                        return r.get("session_id")
                    try:
                        return r["session_id"]  # type: ignore[index]
                    except (KeyError, TypeError, IndexError):
                        return getattr(r, "session_id", None)

                rows = [r for r in rows if _sid(r) == session_id][:5]
            except TypeError:
                # Older signature without kind kwarg — fall through.
                rows = []
            except Exception:
                rows = []
            digest[label] = [_row_to_hit(r) for r in rows]
    except Exception as e:
        log.exception("get_session_digest() failed")
        return {"error": f"get_session_digest failed: {e!r}"}
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    return digest


@mcp.tool(title="Search Messages", annotations=ToolAnnotations(readOnlyHint=True))
@_traced("mcp.search_messages")
def search_messages(
    query: str,
    cwd: str | None = None,
    role: Literal["any", "user", "assistant"] = "any",
    limit: int = 10,
) -> list[dict]:
    """
    Raw FTS5 search over past message text (not extracted memories). Use as fallback when
    `recall` doesn't surface what you want. When `cwd` is omitted the tool searches the
    current cwd first; if that yields 0 hits it automatically retries across all projects
    and prepends a `_meta` row. Returns message hits with role, text snippet, session_id,
    cwd, and ts.
    """
    conn = get_conn()
    if conn is None:
        return _index_missing_error()

    query_mod = _load_query_module()
    if query_mod is None:
        return [{"error": "index.query module not available on this branch"}]

    explicit_cwd = cwd is not None
    target_cwd = cwd if explicit_cwd else _current_cwd()
    role_filter = None if role == "any" else role
    safe_limit = _clamp_limit(limit)

    def _run(cwd_filter: str | None):
        return query_mod.search_messages(
            conn,
            query=query,
            cwd=cwd_filter,
            role=role_filter,
            limit=safe_limit,
        )

    try:
        rows = _run(target_cwd)
        fell_back = False
        scope_used = "explicit_cwd" if explicit_cwd else "this_cwd"
        if not rows and not explicit_cwd:
            rows = _run(None)
            fell_back = True
            scope_used = "all_projects"
    except Exception as e:
        log.exception("search_messages() failed")
        return [{"error": f"search_messages failed: {e!r}"}]
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out = [_row_to_hit(r) for r in rows]
    if fell_back:
        out.insert(0, _scope_meta(scope_used, True, "this_cwd"))
    return _bound_response(out)


__all__ = [
    "recall",
    "prior_sessions_for_cwd",
    "find_failed_attempts",
    "find_user_preferences",
    "get_session_digest",
    "search_messages",
]
