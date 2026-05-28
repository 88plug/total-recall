"""``total-recall rebuild`` — DROP + recreate the index, then full ingest.

Destructive. Always prompts unless ``--yes``. Useful after a schema bump or
when an extractor's output format changes (the UNIQUE(kind, source_uuid)
constraint stops idempotent re-extraction from picking up the new content
otherwise).
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from .util import resolve_db_path


@click.command(help="DESTRUCTIVE: drop the index, recreate the schema, then full ingest.")
@click.option("--yes", is_flag=True, help="Skip the y/N prompt.")
@click.option(
    "--projects-root",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Override the ~/.claude/projects root.",
)
@click.option(
    "--keep-file",
    is_flag=True,
    help="Drop tables instead of deleting the DB file. Preserves perms / inode.",
)
@click.pass_context
def rebuild_cmd(
    ctx: click.Context,
    yes: bool,
    projects_root: str | None,
    keep_file: bool,
) -> None:
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    verbose = bool(ctx.obj.get("verbose"))

    if not yes:
        click.echo(f"This will WIPE the total-recall index at: {db_path}")
        click.confirm("Continue?", abort=True, default=False)

    try:
        from index.db import apply_schema, connect  # type: ignore[import-not-found]
        from index.ingest import ingest_all  # type: ignore[import-not-found]
    except ImportError as exc:
        click.echo(
            f"total-recall: index module not yet available ({exc}). "
            "Run `pip install -e .` after merge.",
            err=True,
        )
        raise click.Abort from exc

    if keep_file and db_path.exists():
        if verbose:
            click.echo(f"[rebuild] dropping tables in {db_path}", err=True)
        conn = connect(db_path)
        try:
            _drop_all_tables(conn)
            apply_schema(conn)
        finally:
            conn.close()
    else:
        # Also remove WAL/SHM siblings so we don't carry stale state.
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                if verbose:
                    click.echo(f"[rebuild] unlink {p}", err=True)
                try:
                    p.unlink()
                except OSError as exc:
                    click.echo(f"total-recall: failed to remove {p}: {exc}", err=True)
                    raise click.Abort from exc
        # Recreate schema by opening a fresh connection.
        connect(db_path).close()

    if verbose:
        click.echo(f"[rebuild] full ingest into {db_path}", err=True)
    started = time.monotonic()
    result = ingest_all(
        db_path=db_path,
        force_full=True,
        cwd_filter=None,
        projects_root=Path(projects_root).expanduser() if projects_root else None,
        dry_run=False,
    )
    # Consolidation pass (cold path). The per-file incremental merge that
    # runs during ingest can freeze an early, non-global winner for
    # frequency-ranked identity scalars (e.g. a handle decided by one file
    # resists later correction via append-supersede). On a full rebuild we
    # have the whole corpus, so re-derive the operator profile in ONE pass
    # over every session and persist the globally-correct values. This is
    # the cold-path reconcile the incremental hot path defers to.
    try:
        from extractors.operator_profile import (  # type: ignore[import-not-found]
            extract_operator_profile,
            persist_profile,
        )

        root = (
            Path(projects_root).expanduser()
            if projects_root
            else Path("~/.claude/projects").expanduser()
        )
        all_jsonl = sorted(root.glob("*/*.jsonl"))
        if all_jsonl:
            full_profile = extract_operator_profile(all_jsonl)
            conn = connect(db_path)
            try:
                persist_profile(conn, full_profile)
                conn.commit()
            finally:
                conn.close()
            if verbose:
                click.echo(
                    f"[rebuild] consolidated operator profile from "
                    f"{len(all_jsonl)} sessions (single full pass)",
                    err=True,
                )
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail the rebuild
        click.echo(f"total-recall: profile consolidation skipped ({exc})", err=True)

    elapsed = time.monotonic() - started

    files = _result_get(result, "files", 0)
    messages = _result_get(result, "messages", 0)
    extractions = _result_get(result, "extractions", 0)

    click.echo(
        f"Rebuilt {db_path}: {files} files, {messages} messages, "
        f"{extractions} extractions, {elapsed:.2f}s."
    )


def _drop_all_tables(conn: object) -> None:
    # Order matters: drop FTS shadow tables first, then triggers/indices, then base.
    statements = [
        "DROP TRIGGER IF EXISTS messages_ai",
        "DROP TRIGGER IF EXISTS messages_ad",
        "DROP TRIGGER IF EXISTS messages_au",
        "DROP TRIGGER IF EXISTS extractions_ai",
        "DROP TRIGGER IF EXISTS extractions_ad",
        "DROP TRIGGER IF EXISTS extractions_au",
        "DROP TABLE IF EXISTS messages_fts",
        "DROP TABLE IF EXISTS extractions_fts",
        "DROP TABLE IF EXISTS extractions",
        "DROP TABLE IF EXISTS messages",
        "DROP TABLE IF EXISTS ingest_state",
        "DROP TABLE IF EXISTS schema_meta",
    ]
    for sql in statements:
        conn.execute(sql)  # type: ignore[attr-defined]


def _result_get(result: object, key: str, default: int) -> int:
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
