"""Static linter: slash-command + skill markdown must not drift from the code.

Three drift classes bit this project repeatedly (dead doc links, `--json` placed
after the subcommand, references to renamed/removed files). This test parses
every `commands/*.md` and `skills/*/SKILL.md` and asserts:

1. Every `${CLAUDE_PLUGIN_ROOT}/<path>` and bare `docs/<file>.md` reference
   points at a file that exists in the repo.
2. Every `recall-cli.sh <subcmd>` / `total-recall <subcmd>` names a real CLI
   subcommand (derived live from the Click group, so new subcommands are
   covered automatically).
3. The global `--json` flag, when present, precedes the subcommand (placing it
   after raises "No such option: --json" at runtime).

Pure text inspection — no subprocess, fast, runs in the normal unit suite.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MD_FILES = sorted(
    [*(REPO / "commands").glob("*.md"), *(REPO / "skills").glob("*/SKILL.md")]
)


def _real_subcommands() -> set[str]:
    from total_recall.__main__ import cli

    return set(cli.commands)


# Only match REAL CLI invocations, never prose. Two reliable signals:
#   * recall-cli.sh <sub>           — the wrapper script; always a CLI call
#   * `total-recall <sub>`          — bare CLI name ONLY inside backticks
# Bare prose like "via the total-recall plugin" must NOT match.
_INVOKE_RE = re.compile(
    r"(?:recall-cli\.sh\"?|`total-recall)\s+((?:--json\s+)?)([a-z][a-z-]+)"
)
_PATH_REF_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")
_DOCS_REF_RE = re.compile(r"(?<!/)\bdocs/[A-Za-z0-9_./-]+\.md\b")


@pytest.mark.parametrize("md", MD_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_referenced_paths_exist(md: Path) -> None:
    text = md.read_text()
    missing = []
    for rel in _PATH_REF_RE.findall(text):
        if not (REPO / rel).exists():
            missing.append(rel)
    for m in _DOCS_REF_RE.findall(text):
        if not (REPO / m).exists():
            missing.append(m)
    assert not missing, f"{md.relative_to(REPO)} references missing files: {missing}"


@pytest.mark.parametrize("md", MD_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_referenced_subcommands_real(md: Path) -> None:
    real = _real_subcommands()
    text = md.read_text()
    bad = []
    for _json_prefix, sub in _INVOKE_RE.findall(text):
        if sub not in real:
            bad.append(sub)
    assert not bad, (
        f"{md.relative_to(REPO)} invokes unknown CLI subcommand(s): {bad}. "
        f"Real subcommands: {sorted(real)}"
    )


@pytest.mark.parametrize("md", MD_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_json_flag_precedes_subcommand(md: Path) -> None:
    """`--json` is a group-level flag; placed after a subcommand it errors."""
    text = md.read_text()
    real = _real_subcommands()
    # Find any "<subcommand> ... --json" on a single invocation line.
    offenders = []
    for line in text.splitlines():
        if "--json" not in line:
            continue
        if "recall-cli.sh" not in line and "total-recall" not in line:
            continue
        # Tokens after the invoker; if a real subcommand appears before --json
        # on the same line, that's the bug.
        m = re.search(r"(?:recall-cli\.sh|total-recall)\"?\s+(.*)", line)
        if not m:
            continue
        tail = m.group(1)
        toks = tail.replace("`", " ").split()
        sub_idx = next((i for i, t in enumerate(toks) if t in real), None)
        json_idx = next((i for i, t in enumerate(toks) if t == "--json"), None)
        if sub_idx is not None and json_idx is not None and json_idx > sub_idx:
            offenders.append(line.strip())
    assert not offenders, (
        f"{md.relative_to(REPO)}: --json must precede the subcommand "
        f"(group-level flag). Offending line(s): {offenders}"
    )
