"""Tests for ``total-recall sources`` CLI subcommand.

The command operates on:

* A hardcoded list of known source names (``KNOWN_SOURCES``).
* A persisted config at ``${CLAUDE_PLUGIN_DATA}/total-recall/sources.json``.
* The :mod:`lib.sources` registry (defensively imported).

Tests are hermetic: ``CLAUDE_PLUGIN_DATA`` is redirected to ``tmp_path``
via a fixture and adapter ``is_available()`` calls are stubbed where
the result actually matters. Subprocess-free — uses Click's
``CliRunner``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from total_recall import cmd_sources
from total_recall.__main__ import cli


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the config dir to ``tmp_path`` so tests don't touch
    the operator's real ``~/.local/share/total-recall``."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    # Belt-and-braces: also override HOME so the fallback path is sandboxed.
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "total-recall" / "sources.json"


# --------------------------------------------------------------------------- #
# CLI plumbing — help, registration
# --------------------------------------------------------------------------- #


def test_sources_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "--help"])
    assert result.exit_code == 0, result.output
    for sub in ("list", "detect", "enable", "disable", "test", "verify"):
        assert sub in result.output, f"`{sub}` missing from sources --help"


def test_sources_is_in_top_level_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "sources" in result.output


@pytest.mark.parametrize(
    "sub", ["list", "detect", "enable", "disable", "test", "verify"]
)
def test_sources_subcommand_help(sub: str) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", sub, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


# --------------------------------------------------------------------------- #
# `sources list`
# --------------------------------------------------------------------------- #


def test_sources_list_human(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "list"])
    assert result.exit_code == 0, result.output
    for name in cmd_sources.KNOWN_SOURCES:
        assert name in result.output


def test_sources_list_json(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "sources" in payload
    assert "config_path" in payload
    names = [r["name"] for r in payload["sources"]]
    assert set(names) == set(cmd_sources.KNOWN_SOURCES)
    # Every row carries the documented keys.
    for row in payload["sources"]:
        assert {"name", "enabled", "registered", "is_available"}.issubset(row)
    # Default config means everything enabled.
    assert all(r["enabled"] for r in payload["sources"])


# --------------------------------------------------------------------------- #
# `sources detect` — uses a stubbed registry
# --------------------------------------------------------------------------- #


class _StubSource:
    """Minimal stand-in for a real SessionSource."""

    def __init__(self, name: str, available: bool, sessions: int = 0) -> None:
        self.name = name
        self._available = available
        self._sessions = sessions

    def is_available(self) -> bool:
        return self._available

    def discover_sessions(self):
        for i in range(self._sessions):
            yield {"i": i}


def _stub_registry(**flags: bool) -> dict:
    """Build a fake registry mapping every known source → stub.

    ``flags`` overrides per-source availability. Sources not listed
    default to ``available=False``.
    """
    out: dict = {}
    for name in cmd_sources.KNOWN_SOURCES:
        out[name] = _StubSource(name, available=flags.get(name, False))
    return out


def test_sources_detect_finds_only_available(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cmd_sources,
        "_load_sources",
        lambda: _stub_registry(claude_code=True, opencode=True),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "detect"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    detected_names = {r["name"] for r in payload["detected"]}
    assert detected_names == {"claude_code", "opencode"}


def test_sources_detect_none_human(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cmd_sources, "_load_sources", lambda: _stub_registry())
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "detect"])
    assert result.exit_code == 0
    assert "No sources" in result.output


# --------------------------------------------------------------------------- #
# `sources enable` / `sources disable`
# --------------------------------------------------------------------------- #


def test_sources_verify_runs_all_known(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "verify"])
    assert result.exit_code == 0, result.output
    for name in cmd_sources.KNOWN_SOURCES:
        assert name in result.output


def test_sources_verify_json_shape(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "verify"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert {s["name"] for s in data["sources"]} == set(cmd_sources.KNOWN_SOURCES)
    for s in data["sources"]:
        assert "is_available" in s
        assert "session_count" in s
        assert "registered" in s


def test_sources_verify_empty_registry(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No adapters registered → command still succeeds; counts unknown."""
    monkeypatch.setattr(cmd_sources, "_load_sources", lambda: {})
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "verify"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    for s in data["sources"]:
        assert s["registered"] is False
        assert s["is_available"] is False


def test_sources_disable_then_enable_round_trip(isolated_config: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["sources", "disable", "opencode"])
    assert res.exit_code == 0, res.output
    assert isolated_config.exists()
    cfg = json.loads(isolated_config.read_text())
    assert "opencode" in cfg["disabled"]
    assert "opencode" not in cfg["enabled"]

    # `sources list` should reflect the change.
    res = runner.invoke(cli, ["--json", "sources", "list"])
    payload = json.loads(res.output)
    opencode = next(r for r in payload["sources"] if r["name"] == "opencode")
    assert opencode["enabled"] is False

    # Re-enable.
    res = runner.invoke(cli, ["sources", "enable", "opencode"])
    assert res.exit_code == 0, res.output
    cfg = json.loads(isolated_config.read_text())
    assert "opencode" in cfg["enabled"]
    assert "opencode" not in cfg["disabled"]


def test_sources_enable_unknown_name_fails(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "enable", "not-a-real-source"])
    assert result.exit_code == 2, result.output


def test_sources_disable_unknown_name_fails(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "disable", "not-a-real-source"])
    assert result.exit_code == 2, result.output


def test_sources_enable_json_payload(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "enable", "cursor"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["name"] == "cursor"
    assert payload["enabled"] is True
    assert payload["config_path"].endswith("sources.json")


# --------------------------------------------------------------------------- #
# `sources test`
# --------------------------------------------------------------------------- #


def test_sources_test_unavailable(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cmd_sources, "_load_sources", lambda: _stub_registry())
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "test", "opencode"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["name"] == "opencode"
    assert payload["registered"] is True
    assert payload["is_available"] is False


def test_sources_test_available_counts_sessions(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _stub_registry()
    reg["claude_code"] = _StubSource("claude_code", available=True, sessions=3)
    monkeypatch.setattr(cmd_sources, "_load_sources", lambda: reg)

    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "sources", "test", "claude_code"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["is_available"] is True
    assert payload["session_count"] == 3


def test_sources_test_unknown_name_fails(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sources", "test", "made-up"])
    assert result.exit_code == 2, result.output


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #


def test_default_config_has_all_known_sources_enabled() -> None:
    cfg = cmd_sources._default_config()
    assert set(cfg["enabled"]) == set(cmd_sources.KNOWN_SOURCES)
    assert cfg["disabled"] == []


def test_is_enabled_handles_missing_disabled_key() -> None:
    # An absent disabled list means everything is enabled.
    assert cmd_sources._is_enabled("opencode", {"enabled": []})
    assert cmd_sources._is_enabled("opencode", {})


def test_load_config_corrupt_file_falls_back(isolated_config: Path) -> None:
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("not valid json {")
    cfg = cmd_sources._load_config()
    assert set(cfg["enabled"]) == set(cmd_sources.KNOWN_SOURCES)
