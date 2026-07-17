"""Small CLI for the optional vector layer.

Examples:
    python -m vec.cli backfill --batch 64 --kinds correction,decision
    python -m vec.cli search "rate limiting in nginx" --limit 5 --cwd /home/me/proj
    python -m vec.cli rebuild

Database path defaults to `~/.claude/total-recall/index.db` (the same path
WT-4's index uses). Override with `--db`.

This module is deliberately thin and standalone — it uses `argparse` rather
than pulling in `click` so it works in any environment.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("TOTAL_RECALL_DB", "~/.claude/total-recall/index.db")).expanduser()


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    # Load sqlite-vec eagerly so every code path that uses this conn (search,
    # backfill, rebuild) can hit the vec0 virtual table without a separate
    # apply_vec_schema() call. If sqlite-vec isn't installed, print a clear
    # install hint and exit — every CLI command depends on it.
    from . import store

    try:
        store._load_sqlite_vec(conn)
    except RuntimeError as exc:
        msg = str(exc)
        if "sqlite-vec is not installed" in msg or "total-recall[vec]" in msg:
            print(
                "error: sqlite-vec not installed; run `pip install total-recall[vec]`",
                file=sys.stderr,
            )
        else:
            print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(1) from exc

    return conn


def _cmd_backfill(args: argparse.Namespace) -> int:
    from .embed import Embedder
    from .store import apply_vec_schema, backfill_all

    only_kinds: list[str] | None = None
    if args.kinds:
        only_kinds = [s.strip() for s in args.kinds.split(",") if s.strip()]

    conn = _open_db(args.db)
    embedder = Embedder()
    apply_vec_schema(
        conn,
        dim=embedder.dim(),
        model=embedder.model,
        backend=embedder.backend,
    )
    report = backfill_all(
        conn,
        embedder=embedder,
        batch_size=args.batch,
        only_kinds=only_kinds,
    )
    print(
        f"backfill complete: seen={report.extractions_seen} "
        f"embedded={report.extractions_embedded} "
        f"skipped={report.extractions_skipped} "
        f"chunks={report.chunks_written}"
    )
    if report.errors:
        print(f"  errors: {len(report.errors)}", file=sys.stderr)
        for err in report.errors[:10]:
            print(f"    - {err}", file=sys.stderr)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from .embed import Embedder
    from .rrf import hybrid_search

    conn = _open_db(args.db)
    embedder = Embedder()
    hits = hybrid_search(
        conn,
        query=args.query,
        embedder=embedder,
        limit=args.limit,
        cwd=args.cwd,
        kind=args.kind,
    )
    if not hits:
        print("(no hits)")
        return 0

    for i, hit in enumerate(hits, 1):
        # Hits may be dicts (from WT-4) or VecHits. Both expose content/kind/cwd.
        if isinstance(hit, dict):
            kind = hit.get("kind", "?")
            cwd = hit.get("cwd", "")
            content = hit.get("content", "")
        else:
            kind = getattr(hit, "kind", "?")
            cwd = getattr(hit, "cwd", "")
            content = getattr(hit, "content", "")
        snippet = (content or "").replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        print(f"{i:2d}. [{kind}] {cwd}")
        print(f"    {snippet}")
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    """Drop vec tables — required when moving to format v2 (ollama-only) or model/dim change."""
    conn = _open_db(args.db)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS vec_chunks")
    cur.execute("DROP TABLE IF EXISTS chunk_embeddings")
    cur.execute("DELETE FROM vec_meta")
    conn.commit()
    print(
        "vec tables dropped (format v2 wipe). "
        "Ensure ollama has an embed model (e.g. `ollama pull qwen3-embedding:0.6b`), "
        "then run `python -m vec.cli backfill`."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vec.cli", description="total-recall vector layer CLI")
    p.add_argument(
        "--db",
        type=lambda s: Path(s).expanduser(),
        default=DEFAULT_DB,
        help=f"Path to index.db (default: {DEFAULT_DB})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backfill", help="Embed all extractions that lack chunks.")
    pb.add_argument("--batch", type=int, default=128)
    pb.add_argument(
        "--kinds",
        type=str,
        default=None,
        help="Comma-separated extraction kinds to restrict to (e.g. 'correction,decision').",
    )
    pb.set_defaults(func=_cmd_backfill)

    ps = sub.add_parser("search", help="Hybrid (FTS5 + dense) search.")
    ps.add_argument("query", type=str)
    ps.add_argument("--limit", type=int, default=10)
    ps.add_argument("--cwd", type=str, default=None)
    ps.add_argument("--kind", type=str, default=None)
    ps.set_defaults(func=_cmd_search)

    pr = sub.add_parser("rebuild", help="Drop the vec tables (used after model/dim change).")
    pr.set_defaults(func=_cmd_rebuild)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
