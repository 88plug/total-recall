#!/usr/bin/env python3
"""Hook → DB bridge for total-recall.

Prints plain markdown to stdout. Honors a soft token budget by truncating at
roughly ``tokens * 4`` characters (the OpenAI-ish heuristic; close enough for
gating). Never raises: any failure prints nothing and exits 0 so the hook can
treat empty stdout as "no memory available".

Subcommands:

* ``signpost`` — short pointer at prior sessions for the current cwd. Emitted
  on SessionStart so the model knows the memory exists without burning the
  context budget on the memory itself.
* ``prompt-relevant`` — per-prompt retrieval. Pulls a few highly-relevant
  memory chunks for a given user prompt + cwd.

Both subcommands defensively import ``index.query`` / ``index.db`` (owned by
WT-4). If those modules aren't on ``sys.path`` yet, we print nothing and exit
0 — the hook surface degrades gracefully to "no memories available".
"""

import argparse
import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# defensive bootstrap
# --------------------------------------------------------------------------- #


def _add_repo_root_to_syspath():
    """Walk up from this file until we find a dir with an ``index`` package.

    The hook is invoked with ``${CLAUDE_PLUGIN_ROOT}`` set, but we don't want
    to depend on it being correct, so we just walk the filesystem.

    Uses ``sys.path.append`` (not ``.insert(0)``) so that an explicit
    ``PYTHONPATH`` (used by tests / overrides) wins over the auto-discovered
    repo root.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "index" / "__init__.py").exists():
            if str(parent) not in sys.path:
                sys.path.append(str(parent))
            return
    # Last resort: also honor CLAUDE_PLUGIN_ROOT if set.
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and (Path(root) / "index" / "__init__.py").exists() and root not in sys.path:
        sys.path.append(root)


_add_repo_root_to_syspath()


def _try_import_query():
    """Return the ``index.query`` module if importable, else ``None``.

    WT-4 owns the contract; we don't know the exact function names yet, so we
    return the module and inspect attributes lazily.
    """
    try:
        import index.query as q  # type: ignore[import-not-found]

        return q
    except Exception:
        return None


def _try_open_db():
    """Open a read-only DB connection or return ``None`` if the DB is missing.

    Read-only because the hook should never be the thing that creates the DB —
    indexing is the indexer's job. If the DB doesn't exist yet, callers should
    treat that as "no memory available".
    """
    try:
        from index.db import DEFAULT_DB_PATH, connect  # type: ignore[import-not-found]

        if not Path(DEFAULT_DB_PATH).exists():
            return None
        return connect(read_only=True)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _truncate_tokens(text: str, max_tokens: int) -> str:
    """Soft-truncate at ~4 chars/token. Adds a trailing ellipsis when cut."""
    if max_tokens <= 0:
        return text
    cap = max_tokens * 4
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def _safe_call(obj, name, *args, **kwargs):
    """Call ``obj.name(*args, **kwargs)`` if it exists, else return ``None``."""
    fn = getattr(obj, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# Small static English stopword set. We strip these from natural-language
# prompts before forming an FTS5 MATCH expression — otherwise a sentence like
# "what did we decide about provider-x" becomes an AND-of-every-word query and
# returns 0 hits because no single extraction contains all six tokens.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "do",
        "did",
        "what",
        "why",
        "how",
        "when",
        "where",
        "we",
        "i",
        "it",
        "this",
        "that",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "about",
        "and",
        "or",
        "but",
        "you",
        "me",
    }
)


def _prompt_to_fts_query(prompt: str) -> str:
    """Turn a free-form user prompt into an FTS5-safe OR-joined MATCH string.

    Steps:
      1. Lowercase + whitespace-tokenize.
      2. Strip ASCII punctuation off each token's edges.
      3. Drop stopwords and tokens shorter than 3 chars.
      4. Quote each surviving token (defang FTS5 operators) and join with
         ``" OR "`` so we match any-token rather than all-tokens.

    Returns ``""`` when no usable tokens remain — callers should treat that
    as "skip the search entirely".
    """
    if not prompt:
        return ""
    # Strip punctuation from token edges; keep internal chars (e.g. "go-1.23").
    raw = prompt.lower().split()
    tokens: list[str] = []
    for t in raw:
        t = t.strip(".,;:!?()[]{}<>\"'`~*_/\\")
        if len(t) < 3:
            continue
        if t in _STOPWORDS:
            continue
        tokens.append(t)
    if not tokens:
        return ""

    # Prefer the index.query helper for quoting if available, so the quoting
    # rules stay consistent with the rest of the codebase.
    q = _try_import_query()
    quoter = getattr(q, "_fts_match_quote", None) if q is not None else None

    quoted: list[str] = []
    for t in tokens:
        if callable(quoter):
            # _fts_match_quote handles one term -> one quoted phrase fine.
            qt = quoter(t)
            if isinstance(qt, str) and qt:
                quoted.append(qt)
        else:
            safe = t.replace('"', '""')
            quoted.append(f'"{safe}"')
    if not quoted:
        return ""
    return " OR ".join(quoted)


# --------------------------------------------------------------------------- #
# subcommand: signpost
# --------------------------------------------------------------------------- #


def cmd_signpost(args):
    """Emit the SessionStart signpost markdown, or nothing if no memory."""
    q = _try_import_query()
    conn = _try_open_db()
    if q is None or conn is None:
        return 0

    # First try a single high-level function if the query layer exposes one.
    # Otherwise compose from the lower-level primitives WT-4 actually ships
    # (session_count_for_cwd / top_topics_for_cwd / search_extractions for
    # standing rules).
    summary = (
        _safe_call(q, "signpost_for_cwd", conn, args.cwd)
        or _safe_call(q, "cwd_signpost", conn, args.cwd)
        or _safe_call(q, "summarize_cwd", conn, args.cwd)
    )

    if summary is None:
        n = _safe_call(q, "session_count_for_cwd", conn, args.cwd) or 0
        topics = _safe_call(q, "top_topics_for_cwd", conn, args.cwd, 3) or []
        # Best-effort "standing rules": pull a couple of `preference`-kind
        # extractions if the search function supports it.
        rules_hits_raw = _safe_call(
            q, "search_extractions", conn, None, args.cwd, "preference", None, None, 2
        )
        rules_hits = rules_hits_raw if isinstance(rules_hits_raw, (list, tuple)) else []
        rules = []
        for h in rules_hits:
            txt = getattr(h, "content", None) or (h.get("content") if isinstance(h, dict) else None)
            if txt:
                rules.append(txt)
        if n or topics or rules:
            summary = {"session_count": n, "topics": topics, "rules": rules}

    if not summary:
        return 0

    # Accept either a pre-formatted string or a dict of parts.
    if isinstance(summary, str):
        text = summary.strip()
    elif isinstance(summary, dict):
        n = summary.get("session_count") or summary.get("n_sessions") or 0
        topics = summary.get("topics") or summary.get("recent_topics") or []
        rules = summary.get("rules") or summary.get("preferences") or []
        if not n and not topics and not rules:
            return 0
        topics_str = ", ".join(str(t) for t in topics[:3]) if topics else "—"
        rules_str = "; ".join(str(r) for r in rules[:2]) if rules else "—"
        text = (
            "**[total-recall]** Memory from {n} prior session{s} "
            "in this cwd available.\n\n"
            "Recent topics here: {topics}.\n\n"
            'Use `recall(topic="...")` MCP tool to expand.\n'
            "Standing rules previously asserted: {rules}."
        ).format(
            n=n,
            s=("s" if n != 1 else ""),
            topics=topics_str,
            rules=rules_str,
        )
    else:
        return 0

    if not text:
        return 0

    sys.stdout.write(_truncate_tokens(text, args.max_tokens))
    return 0


# --------------------------------------------------------------------------- #
# subcommand: prompt-relevant
# --------------------------------------------------------------------------- #


def _try_hybrid_hits(conn, prompt: str, cwd: str | None, limit: int):
    """Dense+FTS hybrid when product ollama embed is available; else None."""
    try:
        from vec.rrf import try_hybrid_search  # type: ignore[import-not-found]

        return try_hybrid_search(conn, prompt, limit=limit, cwd=cwd)
    except Exception:
        return None


def cmd_prompt_relevant(args):
    """Emit per-prompt retrieval markdown, or nothing if no good hits."""
    q = _try_import_query()
    conn = _try_open_db()
    if q is None or conn is None:
        return 0
    if not args.prompt or len(args.prompt.strip()) < 10:
        return 0

    # Prefer hybrid (dense + FTS) with the natural-language prompt — same path
    # as MCP recall(). FTS-only left misses paraphrase / "total recall" intent.
    hits = _try_hybrid_hits(conn, args.prompt.strip(), args.cwd, args.limit)

    # FTS5 MATCH wants either a single token or OR-joined quoted tokens; a raw
    # whitespace-separated user sentence parses as AND-of-all-words and almost
    # never matches a single extraction row. Convert here.
    fts_query = _prompt_to_fts_query(args.prompt)

    # Higher-level wrappers (if WT-4 ever ships one) take a natural-language
    # prompt; the lower-level FTS-backed primitives need the OR-joined form.
    if not hits:
        hits = _safe_call(q, "search_relevant", conn, args.prompt, args.cwd, args.limit) or _safe_call(
            q, "prompt_relevant", conn, args.prompt, args.cwd, args.limit
        )
    if not hits and fts_query:
        # WT-4's real signature: search_extractions(conn, query, cwd, kind, scope, since, limit).
        hits = (
            _safe_call(
                q,
                "search_extractions",
                conn,
                fts_query,
                args.cwd,
                None,
                None,
                None,
                args.limit,
            )
            or _safe_call(q, "search_messages", conn, fts_query, args.cwd, None, args.limit)
            or _safe_call(q, "search", conn, fts_query, args.limit)
        )
    if not hits or not isinstance(hits, (list, tuple)):
        return 0

    lines = ["**[total-recall]** Possibly-relevant prior context:\n"]
    for h in hits:
        # Accept both QueryHit-style dataclasses and dict rows.
        if hasattr(h, "__dict__") and not isinstance(h, dict):
            d = {k: getattr(h, k, None) for k in dir(h) if not k.startswith("_")}
        elif isinstance(h, dict):
            d = h
        else:
            lines.append("- " + str(h))
            continue
        title = (
            d.get("title")
            or d.get("session_title")
            or d.get("topic")
            or d.get("kind")
            or "(untitled)"
        )
        snippet = d.get("snippet") or d.get("text") or d.get("content") or ""
        score = d.get("score")
        score_str = ""
        if isinstance(score, (int, float)):
            score_str = f" (score={score:.2f})"
        lines.append((f"- **{title}**{score_str}: {snippet}").rstrip())

    text = "\n".join(lines).strip()
    if len(text) <= len(lines[0].strip()):
        return 0  # only the preamble survived
    sys.stdout.write(_truncate_tokens(text, args.max_tokens))
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv=None):
    p = argparse.ArgumentParser(prog="total-recall-query")
    # `required=` on subparsers landed in 3.7; we set it via attribute for 3.6.
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    sp = sub.add_parser("signpost")
    sp.add_argument("--cwd", required=True)
    sp.add_argument("--max-tokens", type=int, default=200)
    sp.set_defaults(func=cmd_signpost)

    pr = sub.add_parser("prompt-relevant")
    pr.add_argument("--prompt", required=True)
    pr.add_argument("--cwd", required=True)
    pr.add_argument("--limit", type=int, default=3)
    pr.add_argument("--max-tokens", type=int, default=400)
    pr.set_defaults(func=cmd_prompt_relevant)

    try:
        args = p.parse_args(argv)
    except SystemExit:
        # argparse exits non-zero on bad args; hooks should never propagate that.
        return 0

    try:
        return args.func(args) or 0
    except Exception:
        # Never let an exception leak — empty stdout means "no memory".
        return 0


if __name__ == "__main__":
    sys.exit(main())
