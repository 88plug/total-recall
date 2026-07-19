"""Canonical index path: one DB for plugin installs; no dual-path invent."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_plugin_env_uses_claude_plugin_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("TOTAL_RECALL_DB_DIR", raising=False)
    monkeypatch.delenv("TOTAL_RECALL_DB", raising=False)
    from index import db as dbmod

    assert dbmod.resolve_data_dir() == (tmp_path / "total-recall").resolve()
    assert dbmod.resolve_db_path() == (tmp_path / "total-recall" / "index.db").resolve()


def test_explicit_db_dir_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv("TOTAL_RECALL_DB_DIR", str(override))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    from index import db as dbmod

    assert dbmod.resolve_data_dir() == override.resolve()


def test_discovers_existing_plugin_index_over_empty_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI without CLAUDE_PLUGIN_DATA must not invent XDG when plugin DB exists."""
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("TOTAL_RECALL_DB_DIR", raising=False)
    monkeypatch.delenv("TOTAL_RECALL_DB", raising=False)

    plugins = tmp_path / "plugins-data"
    install = plugins / "total-recall-88plug" / "total-recall"
    install.mkdir(parents=True)
    db_file = install / "index.db"
    db_file.write_bytes(b"x" * 1000)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Point discovery at our fake plugins tree by patching expanduser root —
    # resolve uses Path("~/.claude/...").expanduser() which reads HOME.
    claude = home / ".claude" / "plugins" / "data" / "total-recall-88plug" / "total-recall"
    claude.mkdir(parents=True)
    (claude / "index.db").write_bytes(b"y" * 5000)

    from index import db as dbmod

    # Force re-import free functions (module already loaded with real HOME in
    # other tests — call resolve which reads env/HOME live).
    got = dbmod.resolve_data_dir()
    assert got == claude.resolve()
    assert got != (home / ".local" / "share" / "total-recall").resolve()


def test_list_index_candidates_finds_plugin_and_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    pdir = home / ".claude" / "plugins" / "data" / "total-recall-88plug" / "total-recall"
    pdir.mkdir(parents=True)
    (pdir / "index.db").write_bytes(b"plugin")
    xdg = home / ".local" / "share" / "total-recall"
    xdg.mkdir(parents=True)
    (xdg / "index.db").write_bytes(b"xdg")

    from index import db as dbmod

    cands = dbmod.list_index_candidates()
    assert any("total-recall-88plug" in str(p) for p in cands)
    assert any(".local/share/total-recall" in str(p) for p in cands)
