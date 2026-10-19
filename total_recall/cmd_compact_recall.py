"""``total-recall compact-recall`` — text for the post-compaction inject."""

from __future__ import annotations

from pathlib import Path

import click

from .compact_recall import render_compact_recall
from .util import resolve_db_path


@click.command(
    "compact-recall",
    help="Print operator context and indexed hits for the turn after compaction.",
)
@click.option("--cwd", "cwd", type=str, default=None, help="Project cwd to scope the recall.")
@click.option("--session", "session_id", type=str, default=None, help="Session id.")
@click.option(
    "--topic",
    type=str,
    default=None,
    help="Search topic. Defaults to the persisted continuation directive.",
)
@click.option("--max-chars", type=int, default=2500, show_default=True)
@click.pass_context
def compact_recall_cmd(
    ctx: click.Context,
    cwd: str | None,
    session_id: str | None,
    topic: str | None,
    max_chars: int,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path") if ctx.obj else None)
    try:
        text = render_compact_recall(
            db_path=Path(db_path),
            cwd=cwd,
            session_id=session_id,
            topic=topic,
            max_chars=max_chars,
        )
    except Exception as exc:  # noqa: BLE001 — hook callers must not see a traceback
        click.echo(f"compact-recall: {exc}", err=True)
        return
    if text:
        click.echo(text)
