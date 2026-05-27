# CI / CD architecture

## `ci.yml` — test matrix

Runs on every push to `main`/`master` and on every pull request.
Concurrent runs on the same ref are cancelled (only the latest matters).

### `test` job — 6 parallel jobs

| OS             | Python |
|----------------|--------|
| ubuntu-latest  | 3.10   |
| ubuntu-latest  | 3.11   |
| ubuntu-latest  | 3.12   |
| macos-latest   | 3.10   |
| macos-latest   | 3.11   |
| macos-latest   | 3.12   |

Each job:
1. Installs the package with dev extras (`pip install -e ".[dev]"`).
2. Runs `ruff check .` for lint.
3. Runs `pytest -q --ignore=tests/integration` (unit tests only; integration
   tests require live Claude API credentials and are excluded from CI).
4. Verifies that the three plugin manifests (`.claude-plugin/plugin.json`,
   `.mcp.json`, `hooks/hooks.json`) are present and valid JSON.
5. Parses YAML frontmatter in every `commands/*.md` file.

### `packaging` job

Runs on `ubuntu-latest` / Python 3.12 on every commit.
Builds the sdist and wheel with `python -m build`, then runs `twine check`
to catch metadata errors before a release attempt.

## `release.yml` — tag-triggered release

Triggered by a push of any `v*` tag (e.g. `v0.2.0`).

### Jobs (sequential: build → pypi-publish + github-release)

**`build`**
1. Verifies the git tag matches the `[project].version` in `pyproject.toml`.
   Fails fast if they diverge, preventing a mismatched release.
2. Validates plugin manifests.
3. Builds sdist + wheel and runs `twine check`.
4. Packs a plugin zip archive (for users who install without pip).
5. Uploads everything to the `dist` artifact for downstream jobs.

**`pypi-publish`**
Downloads the `dist` artifact and publishes to PyPI using
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — no
`PYPI_TOKEN` secret is needed once configured.

**`github-release`**
Creates a GitHub release, attaching all `dist/` files and extracting the
relevant section from `CHANGELOG.md` as the release body.

## One-time PyPI trusted publishing setup

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Add a new trusted publisher with:
   - **PyPI project name**: `total-recall`
   - **Owner**: your GitHub org/user
   - **Repository**: `total-recall`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi`
3. Create a GitHub environment named `pypi` in repo Settings → Environments.

No secrets required after that — OIDC handles authentication automatically.

### Manual fallback (trusted publishing not yet configured)

If you need to publish before the trusted publisher is set up:

1. Generate an API token at <https://pypi.org/manage/account/>.
2. Add it as a repo secret named `PYPI_API_TOKEN`.
3. In `release.yml`, change the publish step to:

```yaml
- uses: pypa/gh-action-pypi-publish@release/v1
  with:
    password: ${{ secrets.PYPI_API_TOKEN }}
```

## Dependabot

- **pip** dependencies: checked weekly.
- **GitHub Actions** action versions: checked monthly.

PRs from Dependabot are auto-assigned to `@88plug` via `CODEOWNERS`.
