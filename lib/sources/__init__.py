"""Pluggable session-source adapters.

This package defines the :class:`SessionSource` ABC and ships concrete
adapters for every CLI client whose sessions total-recall knows how to
ingest. Adapters live one-per-module and self-register with the
:data:`SOURCES` list at import time.

To add a new adapter:

1. Create ``lib/sources/<name>.py`` with a class that subclasses
   :class:`SessionSource` and sets ``name = "<name>"``.
2. Implement :meth:`SessionSource.is_available`,
   :meth:`SessionSource.discover_sessions`, and
   :meth:`SessionSource.iter_records`.
3. Append the class to :data:`SOURCES` at module bottom.
4. Import the module in this ``__init__`` so registration happens.

Downstream code should reach for :func:`all_sources` or
:func:`source_by_name` rather than importing concrete classes directly.
"""

from __future__ import annotations

# ABC + registry surface.
from lib.sources.base import (
    SOURCES,
    SessionFile,
    SessionSource,
    all_sources,
    source_by_name,
)

# Bundled adapters — import for side-effect registration. Each adapter
# self-registers via SOURCES.append at module import time, so any code path
# that calls all_sources() / source_by_name() sees the full set. Defensive
# imports so a single broken adapter never breaks the registry.
try:
    from lib.sources.claude_code import ClaudeCodeSource  # noqa: F401
except Exception:  # pragma: no cover
    ClaudeCodeSource = None  # type: ignore[assignment]
try:
    from lib.sources.opencode import OpenCodeSource  # noqa: F401
except Exception:  # pragma: no cover
    OpenCodeSource = None  # type: ignore[assignment]
try:
    from lib.sources.gemini_cli import GeminiCliSource  # noqa: F401
except Exception:  # pragma: no cover
    GeminiCliSource = None  # type: ignore[assignment]
try:
    from lib.sources.codex import CodexSource  # noqa: F401
except Exception:  # pragma: no cover
    CodexSource = None  # type: ignore[assignment]
try:
    from lib.sources.cursor import CursorSource  # noqa: F401
except Exception:  # pragma: no cover
    CursorSource = None  # type: ignore[assignment]
try:
    from lib.sources.continue_dev import ContinueSource  # noqa: F401
except Exception:  # pragma: no cover
    ContinueSource = None  # type: ignore[assignment]
try:
    from lib.sources.cline import ClineSource  # noqa: F401
except Exception:  # pragma: no cover
    ClineSource = None  # type: ignore[assignment]
try:
    from lib.sources.aider import AiderSource  # noqa: F401
except Exception:  # pragma: no cover
    AiderSource = None  # type: ignore[assignment]

__all__ = [
    "SessionFile",
    "SessionSource",
    "SOURCES",
    "all_sources",
    "source_by_name",
    "ClaudeCodeSource",
    "OpenCodeSource",
    "GeminiCliSource",
    "CodexSource",
    "CursorSource",
    "ContinueSource",
    "ClineSource",
    "AiderSource",
]
