"""Packaging sanity tests.

Verifies that the sdist/wheel builds correctly and that key metadata is
consistent. Marked ``slow`` when they invoke the build system; skippable via
``SKIP_SLOW_TESTS=1 pytest``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SKIP_SLOW = os.environ.get("SKIP_SLOW_TESTS", "").strip() not in ("", "0")


# ── Fast metadata tests (no build needed) ─────────────────────────────────

def test_pyproject_version_matches_package_version() -> None:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    toml_version: str = data["project"]["version"]

    # Import __version__ without pulling in optional heavy deps
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "total_recall", ROOT / "total_recall" / "__init__.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    pkg_version: str = mod.__version__  # type: ignore[attr-defined]

    assert toml_version == pkg_version, (
        f"pyproject.toml version ({toml_version}) != "
        f"total_recall.__version__ ({pkg_version})"
    )


def test_pyproject_declares_both_scripts() -> None:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    scripts: dict[str, str] = data["project"]["scripts"]
    assert "total-recall" in scripts, "total-recall entry point missing"
    assert "total-recall-mcp" in scripts, "total-recall-mcp entry point missing"


def test_readme_exists_and_is_referenced() -> None:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    readme_path = data["project"].get("readme", "")
    assert readme_path, "No readme declared in pyproject.toml"
    assert (ROOT / readme_path).exists(), f"README file not found: {readme_path}"


# ── Slow build tests ───────────────────────────────────────────────────────

@pytest.mark.slow
@pytest.mark.skipif(SKIP_SLOW, reason="SKIP_SLOW_TESTS=1")
def test_wheel_builds_successfully(tmp_path: Path) -> None:
    """Build a wheel into tmp_path and verify it completes without error."""
    result = subprocess.run(
        [
            sys.executable, "-m", "build",
            "--wheel",
            "--outdir", str(tmp_path),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"Expected 1 wheel, found: {wheels}"


@pytest.mark.slow
@pytest.mark.skipif(SKIP_SLOW, reason="SKIP_SLOW_TESTS=1")
def test_wheel_contains_all_top_level_packages(tmp_path: Path) -> None:
    """All 6 top-level packages must be present in the wheel."""
    result = subprocess.run(
        [
            sys.executable, "-m", "build",
            "--wheel",
            "--outdir", str(tmp_path),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "No wheel produced"
    wheel = wheels[0]

    expected_packages = {
        "lib",
        "extractors",
        "index",
        "vec",
        "mcp_server",
        "total_recall",
    }

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    found_packages: set[str] = set()
    for name in names:
        # Match top-level package dirs: e.g. "lib/__init__.py" or "lib/foo.py"
        parts = Path(name).parts
        if len(parts) >= 2 and parts[0] in expected_packages:
            found_packages.add(parts[0])

    missing = expected_packages - found_packages
    assert not missing, (
        f"Wheel is missing top-level packages: {missing}\n"
        f"Wheel contents (first 30): {names[:30]}"
    )


@pytest.mark.slow
@pytest.mark.skipif(SKIP_SLOW, reason="SKIP_SLOW_TESTS=1")
def test_twine_check_passes(tmp_path: Path) -> None:
    """twine check must report PASSED for both sdist and wheel."""
    build_result = subprocess.run(
        [
            sys.executable, "-m", "build",
            "--sdist", "--wheel",
            "--outdir", str(tmp_path),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert build_result.returncode == 0, build_result.stderr

    check_result = subprocess.run(
        [sys.executable, "-m", "twine", "check"] + [str(p) for p in tmp_path.iterdir()],
        capture_output=True,
        text=True,
    )
    output = check_result.stdout + check_result.stderr
    assert check_result.returncode == 0, f"twine check failed:\n{output}"
    assert "PASSED" in output, f"Expected PASSED in twine output:\n{output}"
    assert "FAILED" not in output, f"twine reported FAILED:\n{output}"
