"""``total-recall query`` — search the extractions index for a topic."""

from __future__ import annotations

import datetime as _dt
from typing import Any

import click

from .util import iso_or_none, resolve_db_path, shorten


def _parse_since(value: str | None) -> int | None:
    """Accept ISO date, ``YYYY-MM-DD``, or unix-seconds."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    try:
        dt = _dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter(
            f"--since must be ISO-8601 or unix-seconds, got {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


@click.command(help="Search extracted memories for a topic.")
@click.argument("topic", required=True)
@click.option("--cwd", "cwd_filter", type=str, default=None, help="Filter by project cwd.")
@click.option("--kind", "kind_filter", type=str, default=None, help="Filter by extraction kind.")
@click.option(
    "--scope",
    type=click.Choice(["project", "global"]),
    default=None,
    help="Filter by extraction scope.",
)
@click.option("--since", "since", type=str, default=None, help="ISO date or unix seconds.")
@click.option("--limit", type=int, default=10, show_default=True, help="Max results.")
@click.option(
    "--vec/--no-vec",
    "use_vec",
    default=False,
    show_default=True,
    help="Use vector (semantic) search when sqlite-vec + fastembed are installed.",
)
@click.pass_context
def query_cmd(
    ctx: click.Context,
    topic: str,
    cwd_filter: str | None,
    kind_filter: str | None,
    scope: str | None,
    since: str | None,
    limit: int,
    use_vec: bool,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    verbose = bool(ctx.obj.get("verbose"))
    as_json = bool(ctx.obj.get("json"))

    if limit <= 0:
        # Graceful empty — useful for "is the CLI alive?" smoke tests.
        if as_json:
            click.echo("[]")
        else:
            click.echo("(no rows)")
        return

    since_ts = _parse_since(since)

    if not db_path.exists():
        # No DB yet → empty result, exit 0 (not an error: pipelines should not
        # fail just because the user hasn't run ingest yet).
        if verbose:
            click.echo(f"[query] no DB at {db_path}", err=True)
        if as_json:
            click.echo("[]")
        else:
            click.echo("(no rows)")
        return

    try:
        from index.db import connect
        from index.query import search_extractions  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    if verbose:
        click.echo(
            f"[query] db={db_path} topic={topic!r} cwd={cwd_filter} kind={kind_filter} "
            f"scope={scope} since={since_ts} limit={limit} vec={use_vec}",
            err=True,
        )

    conn = connect(db_path, read_only=True)
    try:
        rows = search_extractions(
            conn,
            query=topic,
            cwd=cwd_filter,
            kind=kind_filter,
            scope=scope,
            since=since_ts,
            limit=limit,
        )
    finally:
        conn.close()

    rows = list(rows or [])

    if as_json:
        import json

        click.echo(json.dumps([_row_to_dict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("(no rows)")
        return

    # Compact human format: one extraction per block.
    for i, row in enumerate(rows, 1):
        d = _row_to_dict(row)
        ts_iso = iso_or_none(d.get("ts"))
        head = (
            f"#{i}  kind={d.get('kind')}  cwd={d.get('cwd')}  "
            f"score={d.get('score')}  ts={ts_iso}"
        )
        click.echo(head)
        click.echo(f"     {shorten(d.get('content'), limit=320)}")
        click.echo("")


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Coerce sqlite3.Row / namedtuple / dataclass / dict to a plain dict."""
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        try:
            return dict(row)
        except Exception:
            pass
    if hasattr(row, "_asdict"):
        return dict(row._asdict())
    import dataclasses
    if dataclasses.is_dataclass(row):
        return dataclasses.asdict(row)
    return {"value": row}
