"""Smoke tests for the total-recall CLI surface.

We mostly want fast assertions that:

* ``--help`` works for every subcommand (catches a broken Click registration
  before the slow path).
* ``version`` prints the package version.
* ``query`` returns gracefully when the DB doesn't exist yet.
* ``index --dry-run`` exits 0 against a synthetic projects root (mocking the
  ``index.ingest`` module so this test doesn't depend on WT-4's branch).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from total_recall import __version__
from total_recall.__main__ import cli

SUBCOMMANDS = [
    "index",
    "query",
    "stats",
    "inspect",
    "tail",
    "dump",
    "rebuild",
    "metrics",
    "version",
]


def test_top_level_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in SUBCOMMANDS:
        assert cmd in result.output, f"{cmd} missing from --help: {result.output}"


@pytest.mark.parametrize("cmd", SUBCOMMANDS)
def test_subcommand_help(cmd: str) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [cmd, "--help"])
    assert result.exit_code == 0, f"`{cmd} --help` failed: {result.output}"
    assert "Usage:" in result.output


def test_version_prints_semver() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_version_json() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "version"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload == {"version": __version__}


def test_top_level_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_query_limit_zero_empty_ok(tmp_path: Path) -> None:
    """A zero limit short-circuits with exit 0 even when there's no DB."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--db", str(tmp_path / "missing.db"), "query", "foo", "--limit", "0"],
    )
    assert result.exit_code == 0, result.output
    assert "no rows" in result.output.lower() or result.output.strip() == "[]"


def test_query_no_db_returns_empty(tmp_path: Path) -> None:
    """Query against a missing DB should be graceful (exit 0, empty)."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--db", str(tmp_path / "missing.db"), "query", "topic", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output


def test_query_no_db_json(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "--db", str(tmp_path / "missing.db"), "query", "topic"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "[]"


def test_stats_no_db(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(tmp_path / "missing.db"), "stats"])
    assert result.exit_code == 0, result.output
    assert "missing.db" in result.output


def test_stats_no_db_json(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "--db", str(tmp_path / "missing.db"), "stats"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["exists"] is False
    assert payload["messages"] == 0


def test_db_flag_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """$TOTAL_RECALL_DB should be picked up when --db is not passed."""
    monkeypatch.setenv("TOTAL_RECALL_DB", str(tmp_path / "from-env.db"))
    runner = CliRunner()
    result = runner.invoke(cli, ["stats"])
    assert result.exit_code == 0, result.output
    assert "from-env.db" in result.output


# --------------------------------------------------------------------------- #
# `index --dry-run` with a mocked index.ingest module
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_ingest(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Install fake `index.ingest` so the CLI doesn't depend on WT-4 yet."""
    state: dict[str, object] = {"called_with": None}

    def fake_ingest_all(**kwargs: object) -> dict[str, int]:
        state["called_with"] = kwargs
        # Pretend we found one file with three messages and one extraction.
        return {"files": 1, "messages": 3, "extractions": 1}

    fake_pkg = types.ModuleType("index")
    fake_ingest = types.ModuleType("index.ingest")
    fake_ingest.ingest_all = fake_ingest_all  # type: ignore[attr-defined]
    fake_pkg.ingest = fake_ingest  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "index", fake_pkg)
    monkeypatch.setitem(sys.modules, "index.ingest", fake_ingest)

    return state


def test_index_dry_run_with_synthetic_projects_root(
    tmp_path: Path, mock_ingest: dict[str, object]
) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    # Synthetic .jsonl file — content doesn't matter because the fake
    # ingest_all just returns hard-coded counts.
    proj = projects_root / "-home-operator-foo"
    proj.mkdir()
    (proj / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        '{"type": "user", "uuid": "u1"}\n'
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--db",
            str(tmp_path / "idx.db"),
            "index",
            "--dry-run",
            "--projects-root",
            str(projects_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Ingested 1 files" in result.output
    assert "3 messages" in result.output
    assert "1 extractions" in result.output

    called = mock_ingest["called_with"]
    assert isinstance(called, dict)
    assert called["dry_run"] is True
    assert str(called["projects_root"]) == str(projects_root)


def test_index_json_output(tmp_path: Path, mock_ingest: dict[str, object]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "--db", str(tmp_path / "i.db"), "index", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files"] == 1
    assert payload["messages"] == 3
    assert payload["extractions"] == 1
    assert payload["dry_run"] is True


def test_index_missing_module_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When `index` isn't importable, exit nonzero with a helpful message."""
    # Force ImportError by setting sentinel to None.
    monkeypatch.setitem(sys.modules, "index", None)
    monkeypatch.setitem(sys.modules, "index.ingest", None)
    runner = CliRunner()
    result = runner.invoke(cli, ["--db", str(tmp_path / "i.db"), "index", "--dry-run"])
    assert result.exit_code != 0
    assert "not yet available" in result.output


# --------------------------------------------------------------------------- #
# util.py spot-checks
# --------------------------------------------------------------------------- #


def test_format_table_empty() -> None:
    from total_recall.util import format_table

    assert format_table([]) == "(no rows)"


def test_format_table_dicts() -> None:
    from total_recall.util import format_table

    out = format_table(
        [{"kind": "decision", "count": 4}, {"kind": "progress", "count": 9}],
        headers=["kind", "count"],
    )
    assert "decision" in out
    assert "progress" in out
    assert "kind" in out


def test_human_bytes() -> None:
    from total_recall.util import human_bytes

    assert human_bytes(0) == "0 B"
    assert human_bytes(500) == "500 B"
    assert "KB" in human_bytes(2048)
    assert "MB" in human_bytes(5 * 1024 * 1024)


def test_resolve_db_path_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from total_recall.util import resolve_db_path

    # TOTAL_RECALL_DB is a file override: data dir = parent, canonical name index.db
    # (see index.db.resolve_data_dir). CLI --db still wins as an exact path.
    monkeypatch.setenv("TOTAL_RECALL_DB", str(tmp_path / "env.db"))
    monkeypatch.delenv("TOTAL_RECALL_DB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("GROK_PLUGIN_DATA", raising=False)
    assert resolve_db_path(str(tmp_path / "cli.db")) == tmp_path / "cli.db"
    assert resolve_db_path(None) == (tmp_path / "index.db").resolve()


# --------------------------------------------------------------------------- #
# `metrics` subcommand (MA3)
# --------------------------------------------------------------------------- #


METRICS_SUBCOMMANDS = ["summary", "cost", "sessions", "topics", "health"]


@pytest.fixture
def mock_index_metrics(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``index.metrics`` so MA3 tests don't depend on MA5."""
    summary_payload = {
        "sessions": 12,
        "projects": 3,
        "compactions": 5,
        "input_tokens": 1_234_567,
        "output_tokens": 89_012,
        "cache_read_pct": 73.4,
        "estimated_cost": 4.21,
        "active_hours": 6.5,
        "wall_hours": 18.2,
        "top_corrections": [
            {"content": "use absolute paths", "count": 7},
            {"content": "no comments unless asked", "count": 4},
        ],
        "busiest_project": {"cwd": "/home/operator/foo", "tokens": 800_000},
        "longest_session": {
            "duration_min": 95.0,
            "tokens": 250_000,
            "ai_title": "refactor metrics pipeline",
        },
    }

    fake_metrics = types.ModuleType("index.metrics")
    fake_metrics.summary = lambda db, **kw: summary_payload  # type: ignore[attr-defined]
    fake_metrics.cost = lambda db, **kw: {  # type: ignore[attr-defined]
        "total": 12.34,
        "by_model": [
            {"model": "sonnet", "input_tokens": 100, "output_tokens": 50, "cost": 10.0},
            {"model": "opus", "input_tokens": 20, "output_tokens": 10, "cost": 2.34},
        ],
    }
    fake_metrics.sessions = lambda db, **kw: [  # type: ignore[attr-defined]
        {
            "session_id": "abcdef1234567890",
            "cwd": "/home/operator/foo",
            "tokens": 50_000,
            "duration_min": 42.0,
            "corrections": 2,
            "ai_title": "metrics groundwork",
        }
    ]
    fake_metrics.topics = lambda db, **kw: [  # type: ignore[attr-defined]
        {"topic": "metrics CLI", "count": 14, "sessions": 5}
    ]
    fake_metrics.health = lambda db, **kw: {  # type: ignore[attr-defined]
        "ingest_runs_24h": 24,
        "hook_fire_rate": 0.98,
        "p95_latency_ms": 12,
        "error_count_24h": 0,
    }

    # Ensure parent ``index`` package exists too.
    existing_index = sys.modules.get("index")
    if existing_index is None or existing_index is None:
        pkg = types.ModuleType("index")
        monkeypatch.setitem(sys.modules, "index", pkg)
        pkg.metrics = fake_metrics  # type: ignore[attr-defined]
    else:
        monkeypatch.setattr(existing_index, "metrics", fake_metrics, raising=False)

    monkeypatch.setitem(sys.modules, "index.metrics", fake_metrics)
    return fake_metrics


def test_metrics_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "--help"])
    assert result.exit_code == 0, result.output
    for sub in METRICS_SUBCOMMANDS:
        assert sub in result.output, f"missing {sub}: {result.output}"


@pytest.mark.parametrize("sub", METRICS_SUBCOMMANDS)
def test_metrics_subcommand_help(sub: str) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", sub, "--help"])
    assert result.exit_code == 0, f"`metrics {sub} --help` failed: {result.output}"
    assert "Usage:" in result.output


def test_metrics_summary_human(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "summary", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "total-recall metrics — past 7d" in result.output
    assert "sessions: 12" in result.output
    assert "1,234,567" in result.output  # comma-grouped input_tokens
    assert "73%" in result.output  # cache_read_pct rounded
    assert "$4.21" in result.output
    assert "use absolute paths" in result.output
    assert "/home/operator/foo" in result.output
    assert "refactor metrics pipeline" in result.output


def test_metrics_summary_json(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "metrics", "summary"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sessions"] == 12
    assert payload["input_tokens"] == 1_234_567
    assert payload["busiest_project"]["cwd"] == "/home/operator/foo"


def test_metrics_cost_human(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "cost"])
    assert result.exit_code == 0, result.output
    assert "$12.34" in result.output
    assert "sonnet" in result.output
    assert "opus" in result.output


def test_metrics_cost_rate_flag(mock_index_metrics) -> None:
    """--rate is parsed and forwarded to ``index.metrics.cost``."""
    received: dict[str, object] = {}

    def fake_cost(db, **kw):
        received.update(kw)
        return {"total": 1.0, "by_model": []}

    mock_index_metrics.cost = fake_cost  # type: ignore[attr-defined]

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["metrics", "cost", "--rate", "sonnet=3/15", "--rate", "opus=15/75"],
    )
    assert result.exit_code == 0, result.output
    rates = received.get("rates")
    assert isinstance(rates, dict)
    assert rates["sonnet"] == (3.0, 15.0)
    assert rates["opus"] == (15.0, 75.0)


def test_metrics_cost_bad_rate(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "cost", "--rate", "garbage"])
    assert result.exit_code != 0
    assert "rate" in result.output.lower()


def test_metrics_sessions_human(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "sessions", "--by", "tokens", "--top", "5"])
    assert result.exit_code == 0, result.output
    assert "abcdef12" in result.output  # truncated session_id
    assert "50,000" in result.output
    assert "metrics groundwork" in result.output


def test_metrics_sessions_json(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "metrics", "sessions"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload[0]["tokens"] == 50_000


def test_metrics_topics_human(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "topics"])
    assert result.exit_code == 0, result.output
    assert "metrics CLI" in result.output
    assert "14" in result.output


def test_metrics_health_human(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "health"])
    assert result.exit_code == 0, result.output
    assert "ingest runs (24h):  24" in result.output
    assert "p95 latency" in result.output


def test_metrics_health_json(mock_index_metrics) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "metrics", "health"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ingest_runs_24h"] == 24
    assert payload["error_count_24h"] == 0


def test_metrics_summary_aborts_when_layer_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``index.metrics`` isn't importable, exit 1 with the friendly hint."""
    monkeypatch.setitem(sys.modules, "index.metrics", None)
    # Also wipe ``index`` so ``from index import metrics`` fails cleanly.
    monkeypatch.setitem(sys.modules, "index", None)
    runner = CliRunner()
    result = runner.invoke(cli, ["metrics", "summary"])
    assert result.exit_code == 1, result.output
    combined = result.output + (result.stderr if result.stderr_bytes else "")
    assert "metrics layer not yet available" in combined
