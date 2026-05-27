"""``total-recall tail`` — long-running polling-mode ingest.

Delegates to :func:`index.tail.tail_loop` when present. Falls back to a naive
in-process poll loop that calls ``ingest_all`` every ``--interval`` seconds so
the command still works before WT-4 lands the dedicated tail helper.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from .util import resolve_db_path


@click.command(help="Run a continuous polling-mode ingest. Use Ctrl-C to stop.")
@click.option(
    "--interval",
    type=float,
    default=30.0,
    show_default=True,
    help="Seconds between polls.",
)
@click.option(
    "--cwd",
    "cwd_filter",
    type=str,
    default=None,
    help="Restrict to one project cwd.",
)
@click.option(
    "--projects-root",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Override the ~/.claude/projects root.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single tick and exit (useful from cron / tests).",
)
@click.pass_context
def tail_cmd(
    ctx: click.Context,
    interval: float,
    cwd_filter: str | None,
    projects_root: str | None,
    once: bool,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    verbose = bool(ctx.obj.get("verbose"))

    # Preferred path: WT-4's dedicated tail loop.
    try:
        from index.tail import tail_loop  # type: ignore[import-not-found]
    except ImportError:
        tail_loop = None

    if tail_loop is not None and not once:
        if verbose:
            click.echo(f"[tail] using index.tail.tail_loop interval={interval}s", err=True)
        try:
            tail_loop(
                db_path=db_path,
                interval=interval,
                cwd_filter=cwd_filter,
                projects_root=Path(projects_root).expanduser() if projects_root else None,
            )
        except KeyboardInterrupt:
            click.echo("\nstopped.", err=True)
        return

    # Fallback loop.
    try:
        from index.ingest import ingest_all  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    def _tick() -> None:
        started = time.monotonic()
        try:
            result = ingest_all(
                db_path=db_path,
                full=False,
                cwd_filter=cwd_filter,
                projects_root=Path(projects_root).expanduser() if projects_root else None,
                dry_run=False,
            )
        except TypeError:
            result = ingest_all(db_path=db_path, full=False)  # type: ignore[call-arg]
        elapsed = time.monotonic() - started
        files = _result_get(result, "files", 0)
        messages = _result_get(result, "messages", 0)
        extractions = _result_get(result, "extractions", 0)
        if verbose or files or messages or extractions:
            click.echo(
                f"[tail] files={files} messages={messages} extractions={extractions} "
                f"elapsed={elapsed:.2f}s",
                err=True,
            )

    if once:
        _tick()
        return

    if verbose:
        click.echo(f"[tail] fallback loop, interval={interval}s, db={db_path}", err=True)
    try:
        while True:
            _tick()
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nstopped.", err=True)


def _result_get(result: object, key: str, default: int) -> int:
    if result is None:
        return default
    if isinstance(result, dict):
        return int(result.get(key, default))
    val = getattr(result, key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
