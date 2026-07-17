"""``total-recall index`` — incremental ingest of session JSONL files.

Hooks (e.g. ``hooks/stop-index.sh``) invoke this as
``python3 -m total_recall index --since-last-tick``. ``--since-last-tick``
is the default mode (it reuses ``ingest_state`` per source file) so the flag
is essentially an alias for "do the normal incremental thing".
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from .cmd_rebuild import _backfill_vectors
from .util import resolve_db_path


@click.command(help="Incrementally ingest session JSONL files into the index.")
@click.option(
    "--full",
    is_flag=True,
    help="Re-scan every source file from byte 0 instead of resuming from ingest_state.",
)
@click.option(
    "--since-last-tick",
    "since_last_tick",
    is_flag=True,
    default=False,
    help="Alias for the default incremental mode. Kept for hook readability.",
)
@click.option(
    "--cwd",
    "cwd_filter",
    type=str,
    default=None,
    help="Restrict ingest to one project cwd (matches messages.cwd literally).",
)
@click.option(
    "--projects-root",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Override the ~/.claude/projects root (mostly for tests).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Walk the files and report counts without writing to the index.",
)
@click.option(
    "--jobs",
    "-j",
    type=int,
    default=None,
    help=(
        "Worker processes for parsing JSONL files. Parsing is CPU-bound "
        "(regex-heavy extractors) and parallelizes well; DB writes always "
        "stay single-threaded (SQLite serializes writers). Default: 1 for "
        "incremental ingest, min(cpu_count, 8) for --full backfill. "
        "Tradeoff: more jobs = faster, but each worker holds an additional "
        "session's worth of records in RAM."
    ),
)
@click.pass_context
def index_cmd(
    ctx: click.Context,
    full: bool,
    since_last_tick: bool,
    cwd_filter: str | None,
    projects_root: str | None,
    dry_run: bool,
    jobs: int | None,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    verbose = bool(ctx.obj.get("verbose"))

    try:
        from index.ingest import ingest_all  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    # Resolve --jobs: explicit wins, else 1 for incremental, capped cpu_count
    # for --full backfill. Cap at 8 to avoid OOM on memory-constrained boxes
    # (each worker can hold one session's records in RAM, ~50-200MB peak).
    if jobs is None:
        jobs = min(os.cpu_count() or 4, 8) if full else 1
    jobs = max(1, int(jobs))

    if verbose:
        click.echo(
            f"[index] db={db_path} full={full} cwd={cwd_filter} "
            f"projects_root={projects_root} dry_run={dry_run} jobs={jobs}",
            err=True,
        )

    # `--since-last-tick` is the default incremental mode — the flag exists
    # purely so hook scripts can be self-documenting. Touch it to keep the
    # linter quiet.
    _ = since_last_tick

    started = time.monotonic()
    try:
        result = ingest_all(
            db_path=db_path,
            force_full=full,
            cwd_filter=cwd_filter,
            projects_root=Path(projects_root).expanduser() if projects_root else None,
            dry_run=dry_run,
            jobs=jobs,
        )
    finally:
        # Always clear the bootstrap lockfile if we were the bootstrap run
        # (the hooks write the lockfile when they detach this CLI invocation).
        try:
            lock = Path(db_path).parent / ".bootstrapping"
            if lock.exists():
                lock.unlink()
        except OSError:
            pass
    elapsed = time.monotonic() - started

    # Opportunistically backfill dense vectors for newly-ingested extractions.
    # Reuses the cold-path helper: it's gated by TOTAL_RECALL_VEC, no-ops when
    # the [vec] extra is absent, is never fatal, and backfill_all only embeds
    # extractions missing from chunk_embeddings (incremental). Safe here because
    # the Stop/PostCompact tick runs this CLI fully detached (setsid+nohup), so
    # embedding never blocks the live session.
    try:
        if not dry_run:
            _backfill_vectors(str(db_path), verbose=verbose)
    except Exception:  # noqa: BLE001 - never let vec work fail an ingest tick
        pass

    files = _result_get(result, "files", 0)
    messages = _result_get(result, "messages", 0)
    extractions = _result_get(result, "extractions", 0)

    if ctx.obj.get("json"):
        import json

        payload = {
            "files": files,
            "messages": messages,
            "extractions": extractions,
            "elapsed_seconds": round(elapsed, 3),
            "db_path": str(db_path),
            "dry_run": dry_run,
            "full": full,
        }
        click.echo(json.dumps(payload, default=str))
    else:
        click.echo(
            f"Ingested {files} files, {messages} messages, {extractions} extractions, "
            f"{elapsed:.2f}s elapsed."
        )


def _result_get(result: object, key: str, default: int) -> int:
    """Pull a counter out of whatever ``ingest_all`` returns.

    Real path: ``ingest_all`` returns a ``list[IngestReport]`` — sum the
    per-file counters into the requested aggregate. Test path: the CLI
    smoke test stubs ``ingest_all`` to return a dict. Either works.
    """
    if result is None:
        return default
    if isinstance(result, dict):
        return int(result.get(key, default))
    if isinstance(result, list):
        if key == "files":
            return len(result)
        if key == "messages":
            return sum(int(getattr(r, "new_messages", 0) or 0) for r in result)
        if key == "extractions":
            return sum(int(getattr(r, "new_extractions", 0) or 0) for r in result)
        return default
    val = getattr(result, key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
