"""Tests for the :class:`SessionSource` ABC and the source registry.

These tests are hermetic — they do not touch ``~/.claude/projects`` or
any other on-disk corpus. Adapter-specific behaviour lives in
``tests/test_sources_<adapter>.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lib.sources.base import (
    SOURCES,
    SessionFile,
    SessionSource,
    all_sources,
    source_by_name,
)

# ---------------------------------------------------------------------------
# SessionFile dataclass shape
# ---------------------------------------------------------------------------


def test_session_file_minimal_construction():
    sf = SessionFile(
        source="claude_code",
        path=Path("/tmp/x.jsonl"),
        cwd="/home/x",
        session_id="abc",
        started_at=None,
        last_modified=1234.5,
    )
    assert sf.source == "claude_code"
    assert sf.path == Path("/tmp/x.jsonl")
    assert sf.cwd == "/home/x"
    assert sf.session_id == "abc"
    assert sf.started_at is None
    assert sf.last_modified == 1234.5
    # extra defaults to a fresh dict — not None, not shared.
    assert sf.extra == {}


def test_session_file_extra_is_per_instance():
    """Defaults shouldn't accidentally share state across instances."""
    a = SessionFile(
        source="x", path=Path("/a"), cwd=None, session_id="a",
        started_at=None, last_modified=0.0,
    )
    b = SessionFile(
        source="x", path=Path("/b"), cwd=None, session_id="b",
        started_at=None, last_modified=0.0,
    )
    a.extra["k"] = "v"
    assert b.extra == {}


def test_session_file_carries_extra():
    sf = SessionFile(
        source="opencode",
        path=Path("/var/lib/opencode.db"),
        cwd="/home/x",
        session_id="row-42",
        started_at=1700000000.0,
        last_modified=1700001000.0,
        extra={"table": "messages", "row_id": 42},
    )
    assert sf.extra == {"table": "messages", "row_id": 42}


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_session_source_is_abstract():
    """Can't instantiate the ABC directly — every method is abstract."""
    with pytest.raises(TypeError):
        SessionSource()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_instantiate():
    class Partial(SessionSource):
        name = "partial"

        def is_available(self) -> bool:
            return False
        # missing discover_sessions, iter_records

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]


def test_complete_subclass_instantiates_and_runs():
    class Fake(SessionSource):
        name = "fake"

        def is_available(self) -> bool:
            return True

        def discover_sessions(self) -> Iterator[SessionFile]:
            yield SessionFile(
                source=self.name,
                path=Path("/tmp/fake.jsonl"),
                cwd="/tmp",
                session_id="fake-1",
                started_at=None,
                last_modified=0.0,
            )

        def iter_records(
            self, session: SessionFile, start_offset: int = 0
        ) -> Iterator[tuple[int, Any]]:
            yield (1, {"hello": "world"})

    src = Fake()
    assert src.is_available() is True
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    assert sessions[0].source == "fake"
    assert sessions[0].session_id == "fake-1"

    records = list(src.iter_records(sessions[0]))
    assert records == [(1, {"hello": "world"})]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_sources_registry_is_a_list():
    assert isinstance(SOURCES, list)
    # Every entry must be a SessionSource *class* (not an instance).
    for cls in SOURCES:
        assert isinstance(cls, type)
        assert issubclass(cls, SessionSource)


def test_claude_code_is_registered():
    """Importing :mod:`lib.sources` should register the bundled adapter."""
    import lib.sources  # noqa: F401 — trigger side-effect registration

    names = [cls.name for cls in SOURCES]
    assert "claude_code" in names


def test_all_sources_returns_instances():
    import lib.sources  # noqa: F401

    instances = all_sources()
    assert len(instances) == len(SOURCES)
    assert all(isinstance(s, SessionSource) for s in instances)


def test_source_by_name_known_and_unknown():
    import lib.sources  # noqa: F401

    cc = source_by_name("claude_code")
    assert cc is not None
    assert cc.name == "claude_code"

    assert source_by_name("nope-not-a-source") is None
