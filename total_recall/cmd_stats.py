"""``total-recall stats`` — quick health view of the index."""

from __future__ import annotations

from typing import Any

import click

from .util import format_table, human_bytes, iso_or_none, resolve_db_path


@click.command(help="Print index stats: row counts, DB size, top cwds, last ingest.")
@click.pass_context
def stats_cmd(ctx: click.Context) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    as_json = bool(ctx.obj.get("json"))

    if not db_path.exists():
        payload: dict[str, Any] = {
            "db_path": str(db_path),
            "exists": False,
            "messages": 0,
            "extractions": 0,
            "sessions": 0,
            "db_bytes": 0,
            "last_ingest_ts": None,
            "kinds": [],
            "top_cwds": [],
        }
        if as_json:
            import json

            click.echo(json.dumps(payload, indent=2, default=str))
        else:
            click.echo(f"No DB at {db_path}. Run `total-recall index` first.")
        return

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
        messages = _scalar(conn, "SELECT COUNT(*) FROM messages")
        extractions = _scalar(conn, "SELECT COUNT(*) FROM extractions")
        sessions = _scalar(conn, "SELECT COUNT(DISTINCT session_id) FROM messages")
        last_ingest = _scalar(
            conn,
            "SELECT COALESCE(MAX(mtime), 0) FROM ingest_state",
        )
        kinds_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM extractions GROUP BY kind ORDER BY n DESC"
        ).fetchall()
        cwd_rows = conn.execute(
            "SELECT COALESCE(cwd,'') AS cwd, COUNT(*) AS n FROM messages "
            "GROUP BY cwd ORDER BY n DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    db_bytes = db_path.stat().st_size
    payload = {
        "db_path": str(db_path),
        "exists": True,
        "messages": messages,
        "extractions": extractions,
        "sessions": sessions,
        "db_bytes": db_bytes,
        "last_ingest_ts": last_ingest or None,
        "last_ingest_iso": iso_or_none(last_ingest) if last_ingest else None,
        "kinds": [{"kind": r["kind"], "count": r["n"]} for r in kinds_rows],
        "top_cwds": [{"cwd": r["cwd"], "count": r["n"]} for r in cwd_rows],
    }

    if as_json:
        import json

        click.echo(json.dumps(payload, indent=2, default=str))
        return

    # Human render.
    click.echo(f"DB:           {db_path}")
    click.echo(f"DB size:      {human_bytes(db_bytes)}")
    click.echo(f"Messages:     {messages}")
    click.echo(f"Extractions:  {extractions}")
    click.echo(f"Sessions:     {sessions}")
    click.echo(f"Last ingest:  {iso_or_none(last_ingest) or '(never)'}")
    click.echo("")
    click.echo("Extractions by kind:")
    click.echo(
        format_table(
            [{"kind": r["kind"] or "", "count": r["n"]} for r in kinds_rows],
            headers=["kind", "count"],
        )
    )
    click.echo("")
    click.echo("Top cwds by message volume:")
    click.echo(
        format_table(
            [{"cwd": r["cwd"], "count": r["n"]} for r in cwd_rows],
            headers=["cwd", "count"],
        )
    )


def _scalar(conn: Any, sql: str) -> int:
    cur = conn.execute(sql)
    row = cur.fetchone()
    if row is None:
        return 0
    val = row[0]
    return int(val) if val is not None else 0
