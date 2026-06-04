"""Path normalization helpers for project-scoped pooling.

A single logical project frequently spans many ``cwd`` values once git
worktrees enter the picture: tooling creates per-task checkouts under
``<repo>/.claude/worktrees/<id>`` (or ``<repo>/.worktrees/<id>``), and each
of those reports a distinct ``cwd``. Treating them as separate projects
fragments recall (the corpus showed ~62% of cwds were worktree paths). The
:func:`project_key` helper collapses any worktree path back to its owning
repository root so memory pools per project.
"""

from __future__ import annotations

# Markers that delimit a worktree checkout. The repo root is everything
# *before* the first occurrence of one of these.
_WORKTREE_MARKERS = ("/.claude/worktrees/", "/.worktrees/")


def project_key(cwd: str | None) -> str | None:
    """Collapse a worktree ``cwd`` to its owning repository root.

    * ``None`` in → ``None`` out.
    * Trailing ``/`` is stripped (unless that would empty the string).
    * The first occurrence of ``/.claude/worktrees/`` or ``/.worktrees/``
      truncates the path *before* the marker.
    * Paths with no marker are returned unchanged (modulo the rstrip), so a
      plain project dir or a plugin cache path is never over-stripped.
    """
    if cwd is None:
        return None
    # Strip a trailing slash, but never reduce "/" to "".
    if len(cwd) > 1 and cwd.endswith("/"):
        cwd = cwd.rstrip("/")
        if not cwd:
            cwd = "/"
    for marker in _WORKTREE_MARKERS:
        idx = cwd.find(marker)
        if idx >= 0:
            return cwd[:idx]
    return cwd
