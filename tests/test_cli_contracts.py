"""Cross-module API-contract + CLI-surface tests.

These guard the class of bug that shipped in v0.12.0 and was caught only by a
human running the command: a call site whose kwargs don't match the callee's
signature (cmd_tail → index.tail.tail_loop / index.ingest.ingest_all), and CLI
subcommands that were never smoke-tested. Every check here is fast (signature
inspection + Click --help), so it runs in the normal unit suite.
"""

from __future__ import annotations

import inspect
import re

import pytest
from click.testing import CliRunner

from total_recall.__main__ import cli

# --------------------------------------------------------------------------- #
# Every registered subcommand must respond to --help (cheap smoke).
# --------------------------------------------------------------------------- #


def _registered_subcommands() -> list[str]:
    return sorted(cli.commands.keys())


def test_all_subcommands_have_help() -> None:
    """Each registered subcommand returns exit 0 on --help.

    Frozen lists drift: v0.8–v0.10 added adaptive/consolidate/sources/llm-model
    that the old SUBCOMMANDS list never covered. Deriving from cli.commands
    means a new subcommand is auto-covered.
    """
    runner = CliRunner()
    for name in _registered_subcommands():
        result = runner.invoke(cli, [name, "--help"])
        assert result.exit_code == 0, f"`{name} --help` failed:\n{result.output}"
        assert "Usage:" in result.output


def test_expected_subcommands_present() -> None:
    """The 13 shipped subcommands are all registered (catches accidental drop)."""
    expected = {
        "index",
        "query",
        "stats",
        "inspect",
        "tail",
        "dump",
        "rebuild",
        "metrics",
        "adaptive",
        "consolidate",
        "sources",
        "llm-model",
        "version",
    }
    missing = expected - set(cli.commands)
    assert not missing, f"subcommands missing from CLI: {missing}"


# --------------------------------------------------------------------------- #
# cmd_tail ↔ index.tail / index.ingest signature contract.
# --------------------------------------------------------------------------- #


def test_tail_loop_signature_matches_call_site() -> None:
    """`cmd_tail` must call `tail_loop(conn, interval=, projects_root=)`.

    The shipped bug passed db_path=/cwd_filter= which tail_loop never accepted,
    crashing every `total-recall tail` invocation.
    """
    from index.tail import tail_loop

    params = set(inspect.signature(tail_loop).parameters)
    # First positional is the connection; these kwargs must exist.
    assert "conn" in params
    assert "interval" in params
    assert "projects_root" in params
    # The bad kwargs from the old call site must NOT be silently accepted.
    assert "db_path" not in params
    assert "cwd_filter" not in params


def test_ingest_all_uses_force_full_not_full() -> None:
    """`cmd_tail._tick` must pass force_full=, not full= (the shipped bug)."""
    from index.ingest import ingest_all

    params = set(inspect.signature(ingest_all).parameters)
    assert "force_full" in params
    assert "full" not in params


def test_cmd_tail_call_site_kwargs_are_valid() -> None:
    """Static guard: the kwargs cmd_tail passes to ingest_all all exist.

    Reads the cmd_tail source and confirms it does not reference the retired
    `full=` kwarg anywhere (the fallback branch had it too).
    """
    import total_recall.cmd_tail as cmd_tail

    src = inspect.getsource(cmd_tail)
    # Match the retired kwarg as a whole token, not the `force_full=` substring.
    assert "force_full=False" in src, "cmd_tail should pass force_full=False"
    assert not re.search(r"(?<![\w_])full=", src), "cmd_tail still passes the retired full= kwarg"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("index.tail") is None,
    reason="index.tail not importable",
)
def test_tail_loop_bounded_runs(tmp_path) -> None:
    """tail_loop(conn, max_iterations=1) completes one bounded tick.

    Exercises the real (conn, ...) calling convention end-to-end against an
    empty synthetic projects root so it stays fast and hermetic.
    """
    from index.db import connect
    from index.tail import tail_loop

    conn = connect(tmp_path / "idx.db")
    projects = tmp_path / "projects"
    projects.mkdir()
    try:
        # Must not raise; one iteration over an empty root.
        tail_loop(conn, interval=0, projects_root=projects, max_iterations=1)
    finally:
        conn.close()
