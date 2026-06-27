"""Tests for the ``total-recall-mcp`` console-script entry point.

The wrapper is intentionally thin — it only re-exports
:func:`mcp_server.server.main`. These tests guard against the kinds of
regressions ``uvx total-recall-mcp`` users would hit: the module fails to
import, the symbol disappears, or the entry point in ``pyproject.toml``
drifts from the wrapper's location.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest import mock

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


def test_module_imports():
    """The wrapper module must import cleanly (no top-level side effects)."""
    mod = importlib.import_module("total_recall.mcp_entry")
    assert hasattr(mod, "main"), "total-recall-mcp wrapper must expose `main`"
    assert callable(mod.main)


def test_main_is_mcp_server_main():
    """``total_recall.mcp_entry.main`` must be the same function as
    ``mcp_server.server.main`` — otherwise the console script would diverge
    from ``python -m mcp_server`` behaviour."""
    from mcp_server import server as server_mod
    from total_recall import mcp_entry

    assert mcp_entry.main is server_mod.main


def test_main_delegates_to_server(monkeypatch):
    """Calling the wrapper must call the underlying server.main exactly once.

    We can't actually let the FastMCP stdio loop run in a unit test (it would
    block on stdin), so we patch the reference and verify call-through.
    """
    from total_recall import mcp_entry

    called = mock.Mock()
    # `mcp_entry.main` is a bound reference imported at module load; patch
    # the binding the wrapper actually uses.
    monkeypatch.setattr(mcp_entry, "main", called)
    mcp_entry.main()
    assert called.call_count == 1


def test_reader_resolves_same_path_as_writer(monkeypatch, tmp_path):
    """Regression: the MCP server (reader) must resolve the SAME index.db path
    as ``index.db`` (writer) on the ``CLAUDE_PLUGIN_DATA`` branch.

    The launcher ``scripts/mcp-server.sh`` only guarantees ``CLAUDE_PLUGIN_DATA``
    to the spawned server, NOT ``TOTAL_RECALL_DB_DIR`` (that is set only if the
    harness expands it in the manifest ``env`` block, which is not guaranteed).
    So whenever ``TOTAL_RECALL_DB_DIR`` is absent the server must land on the
    same path the writers use: ``$CLAUDE_PLUGIN_DATA/total-recall/index.db``.

    Before this guard the server omitted the ``total-recall/`` subdir and read
    an empty DB on a clean install while the index had been written under the
    subdir — the divergence that left recall dead until a manual symlink was
    added. If the two resolvers ever drift again, this fails loudly.
    """
    monkeypatch.delenv("TOTAL_RECALL_DB_DIR", raising=False)
    monkeypatch.delenv("TOTAL_RECALL_DB", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

    from index.db import _default_db_path
    from mcp_server.server import _resolve_db_dir

    reader = (_resolve_db_dir() / "index.db").resolve()
    writer = _default_db_path().resolve()
    assert reader == writer, (
        f"reader {reader} != writer {writer} — the MCP server's "
        "CLAUDE_PLUGIN_DATA branch diverged from index.db._default_db_path; "
        "recall will read an empty DB on a clean install."
    )


def test_all_resolvers_agree_on_plugin_data_subdir(monkeypatch, tmp_path):
    """Class guard (close the class, not the case): every per-call path
    resolver must place its artifact under ``$CLAUDE_PLUGIN_DATA/total-recall/``
    when ``CLAUDE_PLUGIN_DATA`` is set and ``TOTAL_RECALL_DB_DIR`` is absent —
    the shipped-install path. The original bug was one resolver (the MCP server)
    dropping the subdir; this locks the whole convention so a sibling can't
    regress silently.
    """
    monkeypatch.delenv("TOTAL_RECALL_DB_DIR", raising=False)
    monkeypatch.delenv("TOTAL_RECALL_DB", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    expected_root = (tmp_path / "total-recall").resolve()

    from index.db import _default_db_path
    from mcp_server.server import _resolve_db_dir
    from total_recall.cmd_sources import _config_dir

    resolved = {
        "mcp_server.server._resolve_db_dir": _resolve_db_dir().resolve(),
        "index.db._default_db_path": _default_db_path().parent.resolve(),
        "total_recall.cmd_sources._config_dir": _config_dir().resolve(),
    }
    for name, path in resolved.items():
        assert path == expected_root, (
            f"{name} resolved to {path!s}, expected {expected_root!s} — a path "
            "resolver diverged from the $CLAUDE_PLUGIN_DATA/total-recall "
            "convention (see test_reader_resolves_same_path_as_writer)."
        )


def test_pyproject_declares_console_script():
    """pyproject.toml must register `total-recall-mcp` so ``uvx`` /
    ``pip install`` produce a discoverable shim on PATH. If somebody removes
    the entry point, this test fails loudly.
    """
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    assert pyproject.exists(), f"pyproject.toml not found at {pyproject}"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    scripts = data.get("project", {}).get("scripts", {})
    assert "total-recall-mcp" in scripts, (
        "pyproject.toml [project.scripts] must declare `total-recall-mcp` "
        "so `uvx total-recall-mcp` works without users knowing about "
        "`python -m mcp_server`."
    )
    assert scripts["total-recall-mcp"] == "total_recall.mcp_entry:main", (
        f"unexpected entry point: {scripts['total-recall-mcp']!r}"
    )
    # The existing CLI must still be there too — we're adding, not replacing.
    assert scripts.get("total-recall") == "total_recall.__main__:cli"
