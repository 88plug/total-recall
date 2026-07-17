"""``total-recall dump`` — export extractions/messages to JSONL or Markdown."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

import click

from .util import iso_or_none, resolve_db_path, shorten


@click.command(help="Export extractions (default) to JSONL or Markdown.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["jsonl", "markdown"]),
    default="markdown",
    show_default=True,
)
@click.option("--kind", "kind_filter", type=str, default=None, help="Filter by kind.")
@click.option("--cwd", "cwd_filter", type=str, default=None, help="Filter by cwd.")
@click.option(
    "--source",
    type=click.Choice(["extractions", "messages"]),
    default="extractions",
    show_default=True,
)
@click.option(
    "--limit",
    type=int,
    default=0,
    help="Cap the number of rows exported (0 = unlimited).",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, writable=True, path_type=str),
    default=None,
    help="Output path; default stdout.",
)
@click.pass_context
def dump_cmd(
    ctx: click.Context,
    fmt: str,
    kind_filter: str | None,
    cwd_filter: str | None,
    source: str,
    limit: int,
    out: str | None,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))

    if not db_path.exists():
        click.echo(f"No DB at {db_path}. Run `total-recall index` first.", err=True)
        raise click.Abort

    try:
        from index.db import connect  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    if source == "extractions":
        sql = (
            "SELECT id, kind, content, session_id, cwd, ts, source_uuid, score, "
            "       scope, context_json "
            "FROM extractions"
        )
    else:
        sql = (
            "SELECT id, session_id, cwd, git_branch, role, kind, ts, "
            "       parent_uuid, message_uuid, byte_offset, source_file, text "
            "FROM messages"
        )

    where: list[str] = []
    params: list[Any] = []
    if kind_filter:
        where.append("kind = ?")
        params.append(kind_filter)
    if cwd_filter:
        where.append("cwd = ?")
        params.append(cwd_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts ASC, id ASC"
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"

    conn = connect(db_path, read_only=True)
    try:
        cur = conn.execute(sql, params)
        sink: TextIO
        if out:
            out_path = Path(out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sink = out_path.open("w", encoding="utf-8")
        else:
            sink = sys.stdout

        try:
            if fmt == "jsonl":
                _write_jsonl(cur, sink)
            else:
                _write_markdown(cur, sink, source=source)
        finally:
            if sink is not sys.stdout:
                sink.close()
    finally:
        conn.close()


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        try:
            return dict(row)
        except Exception:
            pass
    return {"value": row}


def _write_jsonl(cur: Any, sink: TextIO) -> None:
    for row in cur:
        d = _row_to_dict(row)
        # raw_json is BLOB and not selected, but text columns may contain
        # control chars — json handles them.
        sink.write(json.dumps(d, default=str, ensure_ascii=False))
        sink.write("\n")


def _write_markdown(cur: Any, sink: TextIO, *, source: str) -> None:
    sink.write(f"# total-recall dump ({source})\n\n")
    count = 0
    for row in cur:
        d = _row_to_dict(row)
        count += 1
        if source == "extractions":
            sink.write(
                f"## #{d.get('id')} · {d.get('kind')} "
                f"· score={d.get('score')} · scope={d.get('scope')}\n\n"
            )
            sink.write(f"- session: `{d.get('session_id')}`\n")
            sink.write(f"- cwd: `{d.get('cwd') or ''}`\n")
            sink.write(f"- ts: {iso_or_none(d.get('ts')) or d.get('ts')}\n")
            sink.write(f"- source_uuid: `{d.get('source_uuid') or ''}`\n\n")
            sink.write("```\n")
            sink.write((d.get("content") or "").rstrip() + "\n")
            sink.write("```\n\n")
        else:  # messages
            sink.write(f"## #{d.get('id')} · {d.get('role')}/{d.get('kind') or '-'}\n\n")
            sink.write(f"- session: `{d.get('session_id')}`\n")
            sink.write(f"- cwd: `{d.get('cwd') or ''}`\n")
            sink.write(f"- ts: {iso_or_none(d.get('ts')) or d.get('ts')}\n")
            sink.write(f"- uuid: `{d.get('message_uuid') or ''}`\n")
            sink.write(f"- parent: `{d.get('parent_uuid') or ''}`\n\n")
            text = (d.get("text") or "").rstrip()
            sink.write("```\n")
            sink.write(shorten(text, limit=4000) + "\n")
            sink.write("```\n\n")
    if count == 0:
        sink.write("_(no rows)_\n")
