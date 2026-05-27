# Release Process

## Pre-flight checklist

1. Tests must be green (excluding integration suite):

   ```bash
   pytest --ignore=tests/integration
   ```

## Bump version

Update the version string in **three places** — keep them in sync:

| File | Key |
|------|-----|
| `pyproject.toml` | `version = "X.Y.Z"` under `[project]` |
| `total_recall/__init__.py` | `__version__ = "X.Y.Z"` |
| `.claude-plugin/plugin.json` | `"version": "X.Y.Z"` |

Add a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` summarising the
changes since the previous release.

## Commit and tag

```bash
git add pyproject.toml total_recall/__init__.py .claude-plugin/plugin.json CHANGELOG.md
git commit -m "chore: release vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
```

## Build

```bash
python -m build          # produces dist/total_recall-X.Y.Z.tar.gz + .whl
```

## Verify artifacts

```bash
twine check dist/*       # both sdist and wheel must report PASSED
```

## Test-install from sdist in a fresh venv

```bash
python -m venv /tmp/cr-release-test
/tmp/cr-release-test/bin/pip install dist/total_recall-X.Y.Z.tar.gz
/tmp/cr-release-test/bin/total-recall --version    # must print X.Y.Z
/tmp/cr-release-test/bin/total-recall-mcp --help 2>/dev/null || echo "(runs as server, no --help)"
```

Also verify the `[vec]` extras install:

```bash
/tmp/cr-release-test/bin/pip install "dist/total_recall-X.Y.Z.tar.gz[vec]"
```

## Publish to PyPI

```bash
# Requires a PyPI token in ~/.pypirc or $PYPI_TOKEN env var
twine upload dist/*
```

Or use the convenience wrapper (handles safety prompts):

```bash
PYPI_TOKEN=pypi-... bash scripts/publish-pypi.sh
```

## Smoke test after publish

```bash
uvx total-recall-mcp           # uvx auto-installs from PyPI
pip install total-recall       # plain install
pip install "total-recall[vec]"  # with fastembed + sqlite-vec extras
```

## Push the tag

```bash
git push origin vX.Y.Z
git push origin main
```
