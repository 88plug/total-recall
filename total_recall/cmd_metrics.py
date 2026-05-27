"""``total-recall metrics`` — insight into your own Claude Code usage.

This module ships ahead of its data layer dependencies — ``index.metrics``
(built by MA5) and ``total_recall.cost`` (built by MA6). All sub-subcommands
import those modules lazily inside the handler and abort cleanly with a
human-readable hint when they aren't present yet.

Sub-subcommands:

* ``summary``  — sessions / tokens / compactions / top corrections
* ``cost``     — token spend, optionally with custom ``--rate`` overrides
* ``sessions`` — top-N sessions by tokens / duration / corrections
* ``topics``   — most-touched topics in the window
* ``health``   — ingest_runs activity, hook fire rate, p95 latency, errors
"""

from __future__ import annotations

from typing import Any

import click

from .util import (
    format_json,
    format_table,
    resolve_db_path,
    shorten,
)


# --------------------------------------------------------------------------- #
# Defensive metrics-layer loader
# --------------------------------------------------------------------------- #


_UNAVAILABLE_MSG = "metrics layer not yet available — run after MA5 lands"


def _load_metrics() -> Any:
    """Import :mod:`index.metrics` or abort with a friendly message."""
    try:
        from index import metrics as _m  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — surface any import-time failure
        click.echo(f"{_UNAVAILABLE_MSG} ({exc.__class__.__name__})", err=True)
        raise click.exceptions.Exit(1) from exc
    return _m


def _load_cost() -> Any:
    """Import :mod:`total_recall.cost` or fall back to ``None`` (caller decides)."""
    try:
        from . import cost as _c  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    return _c


# --------------------------------------------------------------------------- #
# Group
# --------------------------------------------------------------------------- #


@click.group(name="metrics", help="Insight into your own Claude Code usage from the local index.")
def metrics_cmd() -> None:
    """Top-level ``metrics`` Click group."""


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


@metrics_cmd.command("summary", help="Sessions / tokens / compactions / top corrections in the window.")
@click.option("--since", default="7d", help="Time window: '7d', '30d', '24h', or YYYY-MM-DD.")
@click.option("--project", default=None, help="Filter to one cwd.")
@click.pass_context
def summary_cmd(ctx: click.Context, since: str, project: str | None) -> None:
    m = _load_metrics()
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    data = m.summary(db_path, since=since, project=project)

    if ctx.obj.get("json"):
        click.echo(format_json(data))
        return

    _print_summary_human(data, since)


def _g(d: dict[str, Any], key: str, default: Any = 0) -> Any:
    """Tolerant getter — MA5's schema is still in flux."""
    val = d.get(key)
    return default if val is None else val


def _print_summary_human(data: dict[str, Any], since: str) -> None:
    click.echo(f"total-recall metrics — past {since}")
    sessions = _g(data, "sessions")
    projects = _g(data, "projects")
    compactions = _g(data, "compactions")
    click.echo(
        f"  sessions: {sessions}   projects: {projects}   compactions: {compactions}"
    )

    in_tok = int(_g(data, "input_tokens"))
    out_tok = int(_g(data, "output_tokens"))
    cache_pct = float(_g(data, "cache_read_pct", 0.0))
    cost = float(_g(data, "estimated_cost", 0.0))
    click.echo(
        f"  tokens:  in {in_tok:,} ({cache_pct:.0f}% cache-read), "
        f"out {out_tok:,}    ~ ${cost:.2f}"
    )

    active_h = float(_g(data, "active_hours", 0.0))
    wall_h = float(_g(data, "wall_hours", 0.0))
    click.echo(f"  active:  {active_h:.1f}h    wall: {wall_h:.1f}h")

    top = data.get("top_corrections") or []
    if top:
        formatted = ",  ".join(
            f'"{shorten(c.get("content", ""), 60)}" x{c.get("count", 0)}'
            for c in top[:3]
        )
        click.echo(f"  top corrections: {formatted}")

    busiest = data.get("busiest_project")
    if busiest:
        click.echo(
            f"  busiest project: {busiest.get('cwd', '?')}  "
            f"({int(busiest.get('tokens', 0)):,} tokens)"
        )

    longest = data.get("longest_session")
    if longest:
        dur = float(longest.get("duration_min", 0.0))
        toks = int(longest.get("tokens", 0))
        title = longest.get("ai_title") or longest.get("title") or ""
        click.echo(
            f"  longest session: {dur:.0f} min, {toks:,} tokens — \"{title}\""
        )


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #


def _parse_rate(spec: str) -> tuple[str, float, float]:
    """Parse ``"sonnet=3/15"`` → ``("sonnet", 3.0, 15.0)`` ($/Mtok in/out)."""
    if "=" not in spec or "/" not in spec.split("=", 1)[1]:
        raise click.BadParameter(
            f"--rate must look like 'model=IN/OUT' (got {spec!r})"
        )
    model, rest = spec.split("=", 1)
    in_s, out_s = rest.split("/", 1)
    try:
        return model.strip(), float(in_s), float(out_s)
    except ValueError as exc:
        raise click.BadParameter(f"invalid number in --rate {spec!r}") from exc


@metrics_cmd.command("cost", help="Token spend, optionally with custom per-model rates.")
@click.option(
    "--rate",
    multiple=True,
    help="Override per-model $/Mtok: 'model=IN/OUT', e.g. --rate sonnet=3/15 --rate opus=15/75.",
)
@click.option("--since", default="30d", help="Time window: '7d', '30d', '24h', or YYYY-MM-DD.")
@click.option("--project", default=None, help="Filter to one cwd.")
@click.pass_context
def cost_subcmd(ctx: click.Context, rate: tuple[str, ...], since: str, project: str | None) -> None:
    m = _load_metrics()
    cost_mod = _load_cost()

    # Build the rates dict. Start with MA6's defaults when available.
    rates: dict[str, tuple[float, float]] = {}
    if cost_mod is not None and hasattr(cost_mod, "DEFAULT_RATES"):
        try:
            rates.update(dict(cost_mod.DEFAULT_RATES))
        except Exception:  # noqa: BLE001
            pass
    for spec in rate:
        model, in_rate, out_rate = _parse_rate(spec)
        rates[model] = (in_rate, out_rate)

    db_path = resolve_db_path(ctx.obj.get("db_path"))
    data = m.cost(db_path, since=since, project=project, rates=rates or None)

    if ctx.obj.get("json"):
        click.echo(format_json(data))
        return

    total = float(_g(data, "total_cost", _g(data, "total", 0.0)))
    click.echo(f"total-recall cost — past {since}    total ~ ${total:.2f}")
    by_model = data.get("by_model") or []
    if by_model:
        rows = [
            {
                "model": r.get("model", ""),
                "in_tokens": f"{int(r.get('input', r.get('input_tokens', 0)) or 0):,}",
                "cache_read": f"{int(r.get('cache_read', 0) or 0):,}",
                "out_tokens": f"{int(r.get('output', r.get('output_tokens', 0)) or 0):,}",
                "cost": f"${float(r.get('cost', 0.0)):.2f}",
            }
            for r in by_model
        ]
        click.echo(format_table(rows, headers=["model", "in_tokens", "cache_read", "out_tokens", "cost"]))
    else:
        click.echo("(no usage in window)")


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


@metrics_cmd.command("sessions", help="Top-N sessions in the window.")
@click.option("--top", default=10, type=int, help="Number of sessions to show.")
@click.option(
    "--by",
    type=click.Choice(["tokens", "duration", "corrections"]),
    default="tokens",
    help="Sort key.",
)
@click.option("--since", default="30d", help="Time window: '7d', '30d', '24h', or YYYY-MM-DD.")
@click.option("--project", default=None, help="Filter to one cwd.")
@click.pass_context
def sessions_cmd(
    ctx: click.Context, top: int, by: str, since: str, project: str | None
) -> None:
    m = _load_metrics()
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    data = m.sessions(db_path, since=since, project=project, by=by, top=top)

    if ctx.obj.get("json"):
        click.echo(format_json(data))
        return

    rows = data if isinstance(data, list) else (data.get("sessions") or [])
    if not rows:
        click.echo("(no sessions in window)")
        return

    table_rows = [
        {
            "session_id": (r.get("session_id") or "")[:8],
            "cwd": shorten(r.get("cwd"), 40),
            "tokens": f"{int(r.get('tokens', 0)):,}",
            "duration_min": f"{float(r.get('duration_min', 0.0)):.0f}",
            "corrections": r.get("corrections", 0),
            "title": shorten(r.get("ai_title") or r.get("title"), 50),
        }
        for r in rows[:top]
    ]
    click.echo(f"total-recall sessions — past {since}, sorted by {by}")
    click.echo(
        format_table(
            table_rows,
            headers=["session_id", "cwd", "tokens", "duration_min", "corrections", "title"],
        )
    )


# --------------------------------------------------------------------------- #
# topics
# --------------------------------------------------------------------------- #


@metrics_cmd.command("topics", help="Most-touched topics in the window.")
@click.option("--since", default="30d", help="Time window: '7d', '30d', '24h', or YYYY-MM-DD.")
@click.option("--limit", default=10, type=int, help="Number of topics to show.")
@click.option("--project", default=None, help="Filter to one cwd.")
@click.pass_context
def topics_cmd(ctx: click.Context, since: str, limit: int, project: str | None) -> None:
    m = _load_metrics()
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    data = m.topics(db_path, since=since, project=project, limit=limit)

    if ctx.obj.get("json"):
        click.echo(format_json(data))
        return

    rows = data if isinstance(data, list) else (data.get("topics") or [])
    if not rows:
        click.echo("(no topics in window)")
        return

    table_rows = [
        {
            "topic": shorten(r.get("topic") or r.get("content"), 60),
            "count": r.get("count", 0),
            "sessions": r.get("sessions", ""),
        }
        for r in rows[:limit]
    ]
    click.echo(f"total-recall topics — past {since}")
    click.echo(format_table(table_rows, headers=["topic", "count", "sessions"]))


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


@metrics_cmd.command("health", help="ingest_runs activity, hook fire rate, p95 latency, errors.")
@click.pass_context
def health_cmd(ctx: click.Context) -> None:
    m = _load_metrics()
    db_path = resolve_db_path(ctx.obj.get("db_path"))
    data = m.health(db_path)

    if ctx.obj.get("json"):
        click.echo(format_json(data))
        return

    click.echo("total-recall health")
    boot = data.get("bootstrap")
    if boot:
        if boot.get("in_progress"):
            click.echo(
                f"  bootstrap:          IN PROGRESS  (pid={boot.get('pid')} "
                f"hook={boot.get('hook')} started={boot.get('started')})"
            )
            tail = (boot.get("log_tail") or "").strip()
            if tail:
                click.echo(f"  last log line:      {tail.splitlines()[-1][:120]}")
        else:
            click.echo(
                f"  bootstrap:          finished (started={boot.get('started')}, "
                "stale lockfile present — safe to remove)"
            )
    click.echo(f"  ingest runs (24h):  {_g(data, 'ingest_runs_24h', _g(data, 'ingest_runs_7d'))}")
    click.echo(f"  hook fire rate:     {_g(data, 'hook_fire_rate', 0.0)}")
    p95 = _g(data, "p95_latency_ms", _g(data, "p95_ingest_ms"))
    click.echo(f"  p95 latency:        {p95} ms")
    click.echo(f"  errors (24h):       {_g(data, 'error_count_24h', _g(data, 'error_count_7d'))}")
    last = data.get("last_ingest_iso") or data.get("last_ingest_ts")
    if last:
        click.echo(f"  last ingest:        {last}")
    msg_total = _g(data, "total_messages", 0)
    if msg_total:
        click.echo(f"  total messages:     {msg_total:,}")
        click.echo(f"  total extractions:  {_g(data, 'total_extractions', 0):,}")
