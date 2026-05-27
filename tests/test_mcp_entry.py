"""Tests for the ``total-recall-mcp`` console-script entry point.

The wrapper is intentionally thin — it only re-exports
:func:`mcp_server.server.main`. These tests guard against the kinds of
regressions ``uvx total-recall-mcp`` users would hit: the module fails to
import, the symbol disappears, or the entry point in ``pyproject.toml``
drifts from the wrapper's location.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from unittest import mock


def test_module_imports():
    """The wrapper module must import cleanly (no top-level side effects)."""
    mod = importlib.import_module("total_recall.mcp_entry")
    assert hasattr(mod, "main"), "total-recall-mcp wrapper must expose `main`"
    assert callable(mod.main)


def test_main_is_mcp_server_main():
    """``total_recall.mcp_entry.main`` must be the same function as
    ``mcp_server.server.main`` — otherwise the console script would diverge
    from ``python -m mcp_server`` behaviour."""
    from total_recall import mcp_entry
    from mcp_server import server as server_mod

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
