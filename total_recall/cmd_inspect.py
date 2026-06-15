"""``total-recall inspect`` — drill into one session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .util import format_table, resolve_db_path, shorten


@click.command(help="Inspect a single session by UUID or path to its .jsonl file.")
@click.argument("target", required=True)
@click.option("--show-records", is_flag=True, help="Dump per-record kind/role counts.")
@click.option("--show-extractions", is_flag=True, help="List every extraction row.")
@click.pass_context
def inspect_cmd(
    ctx: click.Context,
    target: str,
    show_records: bool,
    show_extractions: bool,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    as_json = bool(ctx.obj.get("json"))

    if not db_path.exists():
        click.echo(f"No DB at {db_path}. Run `total-recall index` first.", err=True)
        raise click.Abort

    session_id = _resolve_session_id(target)

    try:
        from index.db import connect  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    conn = connect(db_path, read_only=True)
    try:
        # Basic counts.
        total = _scalar(
            conn, "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        )
        if total == 0:
            click.echo(f"No messages for session_id={session_id!r}.", err=True)
            raise click.Abort

        ai_title = _scalar_text(
            conn,
            "SELECT text FROM messages WHERE session_id = ? AND kind = 'ai-title' "
            "ORDER BY ts ASC LIMIT 1",
            (session_id,),
        )

        branch_count = _scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            "  SELECT parent_uuid FROM messages WHERE session_id = ? "
            "   AND parent_uuid IS NOT NULL "
            "   GROUP BY parent_uuid HAVING COUNT(*) > 1"
            ")",
            (session_id,),
        )

        # is_sidechain isn't a column — derive from raw_json best-effort.
        sidechain_count = _scalar(
            conn,
            "SELECT COUNT(*) FROM messages WHERE session_id = ? "
            "AND raw_json IS NOT NULL "
            "AND instr(CAST(raw_json AS TEXT), '\"isSidechain\":true') > 0",
            (session_id,),
        )

        kind_rows = conn.execute(
            "SELECT COALESCE(kind,'') AS kind, COUNT(*) AS n FROM messages "
            "WHERE session_id = ? GROUP BY kind ORDER BY n DESC",
            (session_id,),
        ).fetchall()

        extractions_total = _scalar(
            conn,
            "SELECT COUNT(*) FROM extractions WHERE session_id = ?",
            (session_id,),
        )
        ex_kind_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM extractions WHERE session_id = ? "
            "GROUP BY kind ORDER BY n DESC",
            (session_id,),
        ).fetchall()

        top_prompts = conn.execute(
            "SELECT text FROM messages WHERE session_id = ? AND role = 'user' "
            "AND text IS NOT NULL AND length(text) > 0 "
            "ORDER BY ts ASC LIMIT 5",
            (session_id,),
        ).fetchall()

        records = []
        if show_records:
            records_raw = conn.execute(
                "SELECT id, role, kind, ts, message_uuid, parent_uuid, "
                "       substr(COALESCE(text,''), 1, 200) AS preview "
                "FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            records = [_row_to_dict(r) for r in records_raw]

        extractions = []
        if show_extractions:
            ex_raw = conn.execute(
                "SELECT id, kind, score, scope, ts, "
                "       substr(content, 1, 320) AS preview "
                "FROM extractions WHERE session_id = ? ORDER BY ts ASC",
                (session_id,),
            ).fetchall()
            extractions = [_row_to_dict(r) for r in ex_raw]
    finally:
        conn.close()

    payload: dict[str, Any] = {
        "session_id": session_id,
        "ai_title": ai_title,
        "messages": total,
        "branches": branch_count,
        "sidechains": sidechain_count,
        "extractions": extractions_total,
        "kinds": [{"kind": r["kind"], "count": r["n"]} for r in kind_rows],
        "extraction_kinds": [{"kind": r["kind"], "count": r["n"]} for r in ex_kind_rows],
        "top_user_prompts": [shorten(r["text"], 200) for r in top_prompts],
    }
    if show_records:
        payload["records"] = records
    if show_extractions:
        payload["extractions_detail"] = extractions

    if as_json:
        import json

        click.echo(json.dumps(payload, indent=2, default=str))
        return

    # Human render.
    click.echo(f"session_id:    {session_id}")
    click.echo(f"ai-title:      {ai_title or '(none)'}")
    click.echo(f"messages:      {total}")
    click.echo(f"branches:      {branch_count}")
    click.echo(f"sidechains:    {sidechain_count}")
    click.echo(f"extractions:   {extractions_total}")
    click.echo("")
    click.echo("Record kinds:")
    click.echo(format_table(payload["kinds"], headers=["kind", "count"]))
    click.echo("")
    click.echo("Extraction kinds:")
    click.echo(format_table(payload["extraction_kinds"], headers=["kind", "count"]))
    click.echo("")
    click.echo("Top 5 user prompts:")
    for i, p in enumerate(payload["top_user_prompts"], 1):
        click.echo(f"  {i}. {p}")
    if show_records:
        click.echo("")
        click.echo("Records:")
        click.echo(
            format_table(
                records,
                headers=["id", "role", "kind", "ts", "message_uuid", "parent_uuid", "preview"],
            )
        )
    if show_extractions:
        click.echo("")
        click.echo("Extractions:")
        click.echo(
            format_table(
                extractions,
                headers=["id", "kind", "score", "scope", "ts", "preview"],
            )
        )


def _resolve_session_id(target: str) -> str:
    """Accept either a raw UUID or a path to ``<session-id>.jsonl``."""
    p = Path(target).expanduser()
    if p.suffix == ".jsonl" or p.exists():
        return p.stem
    return target


def _scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return 0
    val = row[0]
    return int(val) if val is not None else 0


def _scalar_text(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> str | None:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        try:
            return dict(row)
        except Exception:
            pass
    return {"value": row}
