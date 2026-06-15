"""``python -m total_recall`` / ``total-recall`` entry point.

All real work is delegated to ``cmd_*`` submodules. This file just wires the
Click group, registers commands, and owns global flags (``--db``, ``--verbose``,
``--json``).
"""

from __future__ import annotations

import sqlite3
import sys

import click

from . import __version__


@click.group(
    help="Cross-session memory for Claude Code. Inspect, query, and rebuild the "
    "total-recall SQLite index built from ~/.claude/projects/*.jsonl.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--db",
    "db_path",
    envvar="TOTAL_RECALL_DB",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    help="Path to the SQLite index. Defaults to "
    "$CLAUDE_PLUGIN_DATA/total-recall/index.db or "
    "~/.local/share/total-recall/index.db.",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging to stderr.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit machine-readable JSON instead of human tables.",
)
@click.version_option(__version__, "-V", "--version", prog_name="total-recall")
@click.pass_context
def cli(ctx: click.Context, db_path: str | None, verbose: bool, as_json: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path
    ctx.obj["verbose"] = verbose
    ctx.obj["json"] = as_json


# ---- subcommand registration ---------------------------------------------- #
# Imports are deferred to function scope inside each cmd_* module, not here,
# so registering the group is essentially free.
from .cmd_adaptive import adaptive_cmd  # noqa: E402
from .cmd_consolidate import consolidate_cmd  # noqa: E402
from .cmd_dump import dump_cmd  # noqa: E402
from .cmd_index import index_cmd  # noqa: E402
from .cmd_inspect import inspect_cmd  # noqa: E402
from .cmd_llm import llm_model_cmd  # noqa: E402
from .cmd_metrics import metrics_cmd  # noqa: E402
from .cmd_query import query_cmd  # noqa: E402
from .cmd_rebuild import rebuild_cmd  # noqa: E402
from .cmd_sources import sources_cmd  # noqa: E402
from .cmd_stats import stats_cmd  # noqa: E402
from .cmd_tail import tail_cmd  # noqa: E402

cli.add_command(index_cmd, name="index")
cli.add_command(query_cmd, name="query")
cli.add_command(stats_cmd, name="stats")
cli.add_command(inspect_cmd, name="inspect")
cli.add_command(tail_cmd, name="tail")
cli.add_command(dump_cmd, name="dump")
cli.add_command(rebuild_cmd, name="rebuild")
cli.add_command(metrics_cmd, name="metrics")
cli.add_command(adaptive_cmd, name="adaptive")
cli.add_command(consolidate_cmd, name="consolidate")
cli.add_command(sources_cmd, name="sources")
cli.add_command(llm_model_cmd, name="llm-model")


@cli.command("version", help="Print the total-recall version.")
@click.pass_context
def version_cmd(ctx: click.Context) -> None:
    if ctx.obj and ctx.obj.get("json"):
        click.echo(f'{{"version": "{__version__}"}}')
    else:
        click.echo(__version__)


def main(argv: list[str] | None = None) -> int:
    """Programmatic entry point; returns the process exit code."""
    try:
        # standalone_mode=False so Click raises instead of sys.exiting — lets
        # us return proper exit codes from tests.
        cli.main(args=argv, standalone_mode=False)
        return 0
    except click.exceptions.Abort:
        return 1
    except click.exceptions.UsageError as exc:
        click.echo(f"Usage error: {exc.format_message()}", err=True)
        return 1
    except click.exceptions.ClickException as exc:
        exc.show()
        return exc.exit_code
    except SystemExit as exc:  # click's --help raises SystemExit(0)
        return int(exc.code) if isinstance(exc.code, int) else 0
    except sqlite3.OperationalError as exc:
        # A "no such table" here means the index file exists but its core
        # schema was never applied — e.g. a DB created only by `adaptive`
        # (which writes just reinjection_outcomes), then read by a query
        # command that opens read-only (so apply_schema is skipped). Treat
        # as "index not built" rather than dumping a raw internal error.
        msg = str(exc)
        if "no such table" in msg:
            click.echo(
                "total-recall: the index has not been built yet "
                f"({msg}). Run `total-recall index --full` first.",
                err=True,
            )
            return 1
        click.echo(f"total-recall: database error: {msg}", err=True)
        return 2
    except Exception as exc:  # pragma: no cover - last-resort guard
        click.echo(f"total-recall: internal error: {exc}", err=True)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
