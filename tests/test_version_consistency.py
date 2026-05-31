"""Every version-bearing file must agree.

A 3-minor-version drift in marketplace-entry.json (0.9.0 while everything else
was 0.12.0) went undetected across the v0.10.x and v0.11.x cycles because no
test compared these files. This pins them together so a release that bumps one
and forgets another fails CI.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _plugin_version() -> str:
    return json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())["version"]


def _marketplace_version() -> str:
    return json.loads((REPO / "marketplace-entry.json").read_text())["version"]


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text()
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert m, "version not found in pyproject.toml"
    return m.group(1)


def _init_version() -> str:
    text = (REPO / "total_recall" / "__init__.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "__version__ not found in total_recall/__init__.py"
    return m.group(1)


def test_all_version_files_agree() -> None:
    versions = {
        "plugin.json": _plugin_version(),
        "marketplace-entry.json": _marketplace_version(),
        "pyproject.toml": _pyproject_version(),
        "total_recall/__init__.py": _init_version(),
    }
    distinct = set(versions.values())
    assert len(distinct) == 1, f"version drift across files: {versions}"
