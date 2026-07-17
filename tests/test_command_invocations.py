"""Guard test: command markdowns must invoke the CLI via the wrapper script.

Bug class B: slash commands invoked the CLI as bare ``total-recall <subcmd>``
or ``python -m total_recall``.  Those patterns fail for installed users because
``total-recall`` is not on PATH and the venv's ``python`` is not the system
python.  The correct pattern is::

    bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh" <subcmd>

This test:
1. Scans every ``commands/*.md`` file.
2. Identifies "actual shell-invocation" lines — lines that live inside backtick
   code spans (inline code) or fenced code blocks, and whose content starts with
   a CLI command token (``total-recall``, ``python``, ``python3``).
3. Asserts that no such line is a bare ``total-recall <subcmd>`` or
   ``python{,3} -m total_recall`` invocation.
4. Asserts that every file that runs the CLI does so via the approved wrapper
   pattern: ``bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh"``.

Prose/description lines that merely mention "total-recall" by name are not
flagged — we key on whether the line looks like it is instructing bash to
RUN a command (i.e. the CLI token is the first meaningful word in a code span
or code block line, not embedded in prose).

NOTE: the sibling agent editing the command markdowns may not have landed all
six files yet.  If this test fails because some files still have bare
invocations, the test output will identify exactly which file/line is
non-compliant — that is EXPECTED until the sibling's edits land.  The test is
written to the TARGET (fully-migrated) state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / "commands"

# ---------------------------------------------------------------------------
# Approved invocation pattern: bash "${CLAUDE_PLUGIN_ROOT}/scripts/recall-cli.sh"
# We accept single-quoted or unquoted variants too, but the canonical form uses
# double-quoted ${CLAUDE_PLUGIN_ROOT}.
# ---------------------------------------------------------------------------

_APPROVED_RE = re.compile(r'bash\s+"?\$\{CLAUDE_PLUGIN_ROOT\}/scripts/recall-cli\.sh"?')

# ---------------------------------------------------------------------------
# Banned raw-invocation patterns.
# These are shell tokens that would execute the CLI directly and fail for
# installed users.
# ---------------------------------------------------------------------------

# Matches "total-recall" at the START of a code-span or code-block line,
# optionally preceded by whitespace, followed by a subcommand or end.
# We reject: `total-recall metrics ...`, `total-recall rebuild`, etc.
_BARE_TOTAL_RECALL_RE = re.compile(
    r"(?:^|\s)`total-recall(?:\s+\S+)?`|"  # inline: `total-recall ...`
    r"(?:^|\s)total-recall\s+\w",  # code block line: total-recall <subcmd>
)

# Matches bare python -m total_recall or python3 -m total_recall in code context.
_BARE_PYTHON_RE = re.compile(r"python3?\s+-m\s+total_recall")


# ---------------------------------------------------------------------------
# Helpers to extract "code-span and code-block" invocation lines from markdown.
# ---------------------------------------------------------------------------


def _extract_invocation_lines(text: str) -> list[tuple[int, str]]:
    """Return (lineno, stripped_content) for lines that look like shell invocations.

    Two sources:
    * Inline backtick spans: ``...``.  We extract the inner content.
    * Fenced code block lines (inside ``` blocks).

    We deliberately skip lines where the CLI token appears only in prose
    (e.g. "suggest running total-recall rebuild" or "see /total-recall:recall-X").
    """
    result: list[tuple[int, str]] = []
    in_fence = False
    lines = text.splitlines()

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()

        # Detect fenced code block toggle.
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        if in_fence:
            # Any non-empty line inside a fence is an invocation candidate.
            if stripped:
                result.append((lineno, stripped))
            continue

        # Outside fences — look for inline backtick spans.
        # Extract every `...` span that starts with a CLI-style token.
        for m in re.finditer(r"`([^`]+)`", raw):
            inner = m.group(1).strip()
            # Only care about spans whose first word looks like a command
            # (total-recall, bash, python, python3).
            first_word = inner.split()[0] if inner.split() else ""
            if first_word in ("total-recall", "bash", "python", "python3"):
                result.append((lineno, inner))

    return result


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=sorted(COMMANDS_DIR.glob("*.md")),
    ids=lambda p: p.name,
)
def command_file(request: pytest.FixtureRequest) -> Path:
    return request.param  # type: ignore[return-value]


def test_command_uses_wrapper_not_bare_cli(command_file: Path) -> None:
    """Each command file that invokes the CLI must use the wrapper, not bare CLI.

    Checks two things:
    1. No bare ``total-recall <subcmd>`` invocations in code context.
    2. No bare ``python{,3} -m total_recall`` invocations.
    3. Any file that runs the CLI uses the approved wrapper pattern.
    """
    text = command_file.read_text(encoding="utf-8")
    invocation_lines = _extract_invocation_lines(text)

    violations: list[str] = []

    for lineno, content in invocation_lines:
        # Check for banned bare total-recall invocations.
        # A line is a "bare invocation" if it starts with total-recall (as a command).
        first_token = content.split()[0] if content.split() else ""
        if first_token == "total-recall":
            violations.append(
                f"  Line {lineno}: bare `total-recall` invocation: {content!r}\n"
                f"  -> Replace with: "
                f'bash "${{CLAUDE_PLUGIN_ROOT}}/scripts/recall-cli.sh" <subcmd>'
            )
            continue

        # Check for banned python -m total_recall invocations.
        if _BARE_PYTHON_RE.search(content):
            violations.append(
                f"  Line {lineno}: bare python invocation: {content!r}\n"
                f"  -> Replace with: "
                f'bash "${{CLAUDE_PLUGIN_ROOT}}/scripts/recall-cli.sh" <subcmd>'
            )
            continue

        # If the line starts with `bash`, verify it uses the approved wrapper.
        if (
            first_token == "bash"
            and "recall-cli.sh" in content
            and not _APPROVED_RE.search(content)
        ):
            violations.append(
                f"  Line {lineno}: bash invocation does not use approved "
                f"wrapper pattern: {content!r}\n"
                f"  -> Approved pattern: "
                f'bash "${{CLAUDE_PLUGIN_ROOT}}/scripts/recall-cli.sh" <subcmd>'
            )

    assert not violations, f"{command_file.name}: CLI invocation violations found:\n" + "\n".join(
        violations
    )


def test_all_command_files_present() -> None:
    """Sanity check: the commands/ directory must contain at least the expected files."""
    expected = {
        "recall.md",
        "recall-cost.md",
        "recall-health.md",
        "recall-metrics.md",
        "recall-rebuild.md",
        "recall-status.md",
        "recall-inspect.md",
        "recall-topics.md",
    }
    found = {p.name for p in COMMANDS_DIR.glob("*.md")}
    missing = expected - found
    assert not missing, f"Missing expected command files: {sorted(missing)}"
