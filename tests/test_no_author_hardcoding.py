"""Leak guard: ensure no operator-private literals are hardcoded in shipped source.

Scans the shipped source directories for forbidden operator-specific strings.
Any match is a regression that would make the plugin operator-specific rather
than generic.

PRIVACY: the forbidden list is operator-private (it names the original author's
machines / infra). It is therefore NOT committed — it lives in the gitignored
``tests/local/author_denylist.json`` (override with ``BT_AUTHOR_DENYLIST``).
When that file is absent (public checkout / CI), this test skips: there is
nothing operator-specific to guard against in a clean public tree, and we must
not publish the very names we are trying to keep private.

Scanned directories (relative to repo root):
    extractors/, mcp_server/, index/, lib/, detector/, hooks/lib/
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ["extractors", "mcp_server", "index", "lib", "detector", "hooks/lib"]
SCAN_EXTENSIONS = {".py"}

_DENYLIST_PATH = Path(
    os.environ.get(
        "BT_AUTHOR_DENYLIST", str(REPO_ROOT / "tests" / "local" / "author_denylist.json")
    )
)


def _load_forbidden() -> list[str]:
    if not _DENYLIST_PATH.is_file():
        return []
    try:
        data = json.loads(_DENYLIST_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [str(s) for s in data.get("forbidden_literals", []) if str(s).strip()]


FORBIDDEN_LITERALS = _load_forbidden()

pytestmark = pytest.mark.skipif(
    not FORBIDDEN_LITERALS,
    reason="no local author denylist (tests/local/author_denylist.json) — nothing private to guard",
)


def _scan_file(
    path: Path, patterns: list[tuple[str, re.Pattern[str]]]
) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for line_no, line in enumerate(text.splitlines(), start=1):
        for literal, pat in patterns:
            if pat.search(line):
                hits.append((line_no, literal, line.strip()))
    return hits


def _collect_hits() -> list[tuple[str, int, str, str]]:
    patterns = [(lit, re.compile(re.escape(lit), re.IGNORECASE)) for lit in FORBIDDEN_LITERALS]
    all_hits: list[tuple[str, int, str, str]] = []
    for rel_dir in SCAN_DIRS:
        scan_path = REPO_ROOT / rel_dir
        if not scan_path.is_dir():
            continue
        for py_file in sorted(scan_path.rglob("*")):
            if py_file.suffix not in SCAN_EXTENSIONS or not py_file.is_file():
                continue
            for line_no, literal, line_text in _scan_file(py_file, patterns):
                all_hits.append((str(py_file.relative_to(REPO_ROOT)), line_no, literal, line_text))
    return all_hits


def test_no_author_literals_in_source() -> None:
    """Fail with a full list of offending file:line hits if any are found."""
    hits = _collect_hits()
    if not hits:
        return
    lines = [f"  {rel}:{line_no}  [{literal!r}]" for rel, line_no, literal, _ in hits]
    pytest.fail(
        f"Found {len(hits)} operator-specific literal(s) in shipped source.\n"
        "These make the plugin operator-specific rather than generic.\n"
        "Offenders:\n" + "\n".join(lines)
    )
