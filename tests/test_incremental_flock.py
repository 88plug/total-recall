"""Regression test for the detached + flock-gated incremental indexer.

Guards the v0.10.1/v0.11.0 fix: the Stop/PostCompact hooks dispatch the
incremental ingest via ``recall::start_incremental_index`` (hooks/lib/common.sh),
which detaches the worker (setsid+nohup) and wraps it in an ``flock -n`` so two
ticks firing in quick succession collapse to one instead of piling up and
racing the SQLite writer.

This drives the real bash helper via subprocess — no Python reimplementation —
so a regression in common.sh is caught here.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "hooks" / "lib" / "common.sh"

pytestmark = pytest.mark.skipif(not COMMON_SH.is_file(), reason="hooks/lib/common.sh not present")


def _has_flock() -> bool:
    try:
        return (
            subprocess.run(
                ["bash", "-c", "command -v flock"],
                capture_output=True,
            ).returncode
            == 0
        )
    except FileNotFoundError:
        return False


def _make_stub_py(path: Path) -> Path:
    """A fake 'python' shim that records each invocation to an output file."""
    out = path / "child.out"
    shim = path / "stub_py"
    shim.write_text(f'#!/usr/bin/env bash\necho "RAN args=$*" >> {out}\n')
    shim.chmod(0o755)
    return shim


def _call_helper(data_root: Path, stub: Path, label: str) -> None:
    """Source common.sh with CLAUDE_PLUGIN_DATA=data_root and dispatch one tick."""
    subprocess.run(
        [
            "bash",
            "-c",
            f'source "{COMMON_SH}"; recall::start_incremental_index "{stub}" "{label}"',
        ],
        env={**os.environ, "CLAUDE_PLUGIN_DATA": str(data_root)},
        check=True,
        capture_output=True,
    )


def test_incremental_tick_dispatches_child(tmp_path: Path) -> None:
    """A single tick detaches a child that runs the python shim once."""
    if not _has_flock():
        pytest.skip("bash/flock not available on this host")
    data_root = tmp_path
    (data_root / "total-recall").mkdir(parents=True, exist_ok=True)
    stub = _make_stub_py(tmp_path)
    out = tmp_path / "child.out"

    _call_helper(data_root, stub, "TestSingle")

    # Detached child — poll briefly for its output.
    for _ in range(40):
        if out.is_file():
            break
        time.sleep(0.1)
    assert out.is_file(), "detached child never ran the indexer shim"
    assert "RAN args=" in out.read_text()


def test_incremental_tick_skips_when_lock_held(tmp_path: Path) -> None:
    """A second tick is skipped (flock -n) while the lock is held by another."""
    if not _has_flock():
        pytest.skip("flock not available on this host")

    data_root = tmp_path
    lock_dir = data_root / "total-recall"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / ".incremental.lock"
    lock_file.touch()
    stub = _make_stub_py(tmp_path)
    out = tmp_path / "child.out"

    # Hold the lock in a long-lived flock subprocess, then fire a tick; the
    # detached child must fail flock -n and exit without running the shim.
    holder = subprocess.Popen(
        ["bash", "-c", f'exec 9>"{lock_file}"; flock -n 9 && sleep 3'],
    )
    try:
        time.sleep(0.4)  # let the holder acquire the lock
        _call_helper(data_root, stub, "TestContended")
        time.sleep(1.5)  # give the (skipped) child time to NOT run
        assert not out.is_file(), "child ran despite the lock being held (flock not honoured)"
    finally:
        holder.wait(timeout=6)
