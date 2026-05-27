"""``total-recall adaptive`` — per-trigger re-injection precision report.

Closes the feedback loop on :mod:`detector.reinjection`. Each fire is
logged into ``reinjection_outcomes`` by the hook layer, then resolved
3 operator turns later as ``useful`` (no callout/insult fired) or
``useless`` (one did). This command surfaces the running Beta posterior
precision per trigger and recommends ``tighten`` / ``loosen`` / ``keep``
based on the resolved evidence.

The DB layer is shared with the main index — we just open the same file
read-only. If the table does not yet exist (no hook has logged anything)
we report an empty table rather than aborting.
"""

from __future__ import annotations

import sqlite3

import click

from .util import format_json, format_table, resolve_db_path


@click.command(name="adaptive")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_context
def adaptive_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show per-trigger re-injection precision and recommended threshold changes.

    \b
    Example output:
      Trigger                          Precision   Useful  Useless  Recommend
      hard:repetition_callout          0.94        145     9        keep
      hard:escalation_risk=5           0.78        62      18       keep
      hard:explicit_recall_phrase      0.91        88      9        keep
      hard:scope_shift_to_known        0.85        102     18       keep
      soft:long_silence_resume         0.52        34      32       tighten
      soft:stale                       0.41        12      17       tighten
      soft:verbose_under_correction    0.83        38      8        keep
    """
    # Honour either the local --json or the global one from the root group.
    as_json = bool(as_json or (ctx.obj and ctx.obj.get("json")))

    from detector.outcomes import all_trigger_stats, ensure_schema

    db_path = resolve_db_path(ctx.obj.get("db_path") if ctx.obj else None)
    if not db_path.exists():
        payload = {"db_path": str(db_path), "exists": False, "triggers": []}
        if as_json:
            click.echo(format_json(payload))
        else:
            click.echo(f"No DB at {db_path}. Run `total-recall index` first.")
        return

    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True)
    try:
        ensure_schema(conn)
        stats = all_trigger_stats(conn)
    finally:
        conn.close()

    if as_json:
        click.echo(
            format_json(
                {
                    "db_path": str(db_path),
                    "triggers": [
                        {
                            "trigger": s.trigger,
                            "precision": round(s.precision, 4),
                            "useful": s.useful,
                            "useless": s.useless,
                            "n": s.n,
                            "recommend": s.recommend,
                        }
                        for s in stats
                    ],
                }
            )
        )
        return

    if not stats:
        click.echo(
            "total-recall adaptive — no resolved re-injections yet.\n"
            "(Hook needs to log fires; outcomes resolve 3 turns later.)"
        )
        return

    rows = [
        {
            "Trigger": s.trigger,
            "Precision": f"{s.precision:.2f}",
            "Useful": s.useful,
            "Useless": s.useless,
            "Recommend": s.recommend,
        }
        for s in stats
    ]
    click.echo("total-recall adaptive — per-trigger re-injection precision")
    click.echo(
        format_table(
            rows,
            headers=["Trigger", "Precision", "Useful", "Useless", "Recommend"],
        )
    )
