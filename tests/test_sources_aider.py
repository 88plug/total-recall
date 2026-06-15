"""Tests for the Aider markdown-history adapter (:mod:`lib.sources.aider`).

Aider's storage is per-repo markdown — no JSONL, no DB, no central
manifest. These tests build synthetic repo trees under ``tmp_path`` so we
can exercise discovery without touching the operator's real home dir.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from lib.schema import AssistantRecord, UserRecord
from lib.sources import SessionFile
from lib.sources.aider import (
    HISTORY_FILENAME,
    SESSION_HEADER,
    USER_LINE,
    AiderSource,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TWO_SESSION_MD = """\
# aider chat started at 2025-01-15 14:23:01

#### Fix any errors below, if possible.
####
#### ## Running: /tmp/lint.sh aider/repomap.py
> isort....................................................................Passed
> black....................................................................Passed
> flake8...................................................................Failed
> aider/repomap.py:153:80: E501 line too long

# aider chat started at 2025-01-16 09:00:00

#### Add a unit test for the new helper.
#### Make sure it covers the empty-input case.
> Sure — I'll add `test_helper_empty` in tests/test_helper.py.
> Here's the patch:
```diff
+ def test_helper_empty():
+     assert helper("") == ""
```
> Let me know if you'd like a different filename.
"""


def _make_aider_repo(root: Path, name: str, content: str) -> Path:
    """Create ``<root>/<name>/.aider.chat.history.md`` with ``content``."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / HISTORY_FILENAME).write_text(content, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# Identity / regex sanity
# ---------------------------------------------------------------------------


def test_name_constant():
    assert AiderSource.name == "aider"
    assert AiderSource().name == "aider"


def test_session_header_regex_matches_real_header():
    m = SESSION_HEADER.match("# aider chat started at 2025-01-15 14:23:01")
    assert m is not None
    assert m.group(1) == "2025-01-15 14:23:01"


def test_session_header_regex_rejects_garbage():
    assert SESSION_HEADER.match("# something else") is None
    assert SESSION_HEADER.match("## aider chat started at 2025-01-15 14:23:01") is None


def test_user_line_regex_matches_blank_and_text():
    assert USER_LINE.match("####").group(1) in (None, "")
    assert USER_LINE.match("#### Hello").group(1) == "Hello"


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_is_available_false_in_empty_tree(tmp_path: Path):
    # Use a search root that has nothing aider-shaped.
    src = AiderSource(search_roots=[tmp_path])
    assert src.is_available() is False


def test_is_available_true_when_history_present(tmp_path: Path):
    _make_aider_repo(tmp_path, "myproj", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    assert src.is_available() is True


def test_is_available_finds_under_projects_subdir(tmp_path: Path):
    _make_aider_repo(tmp_path / "projects", "subproj", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    assert src.is_available() is True


def test_is_available_skips_pruned_dirs(tmp_path: Path):
    # node_modules is pruned at depth 1 — should NOT trip is_available.
    _make_aider_repo(tmp_path / "node_modules", "evil", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    assert src.is_available() is False


# ---------------------------------------------------------------------------
# Session splitting
# ---------------------------------------------------------------------------


def test_split_sessions_finds_two_headers(tmp_path: Path):
    repo = _make_aider_repo(tmp_path, "split", TWO_SESSION_MD)
    md = repo / HISTORY_FILENAME
    out = AiderSource._split_sessions(md)
    assert len(out) == 2
    assert out[0]["ts"] == datetime(2025, 1, 15, 14, 23, 1)
    assert out[1]["ts"] == datetime(2025, 1, 16, 9, 0, 0)
    # Offsets are contiguous and cover the file.
    assert out[0]["start"] == 0
    assert out[0]["end"] == out[1]["start"]
    assert out[1]["end"] == md.stat().st_size


def test_split_sessions_empty_file(tmp_path: Path):
    repo = _make_aider_repo(tmp_path, "blank", "")
    out = AiderSource._split_sessions(repo / HISTORY_FILENAME)
    assert out == []


def test_split_sessions_no_headers(tmp_path: Path):
    repo = _make_aider_repo(tmp_path, "noheader", "#### orphan turn\n> orphan reply\n")
    out = AiderSource._split_sessions(repo / HISTORY_FILENAME)
    assert out == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_two_per_two_session_file(tmp_path: Path):
    _make_aider_repo(tmp_path, "myproj", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    sessions = list(src.discover_sessions())
    assert len(sessions) == 2
    assert all(isinstance(s, SessionFile) for s in sessions)
    assert {s.source for s in sessions} == {"aider"}

    # Session ids embed repo name and ISO timestamp.
    ids = sorted(s.session_id for s in sessions)
    assert ids[0].startswith("myproj:")
    assert ids[1].startswith("myproj:")

    # cwd is the repo root.
    for sf in sessions:
        assert sf.cwd == str(tmp_path / "myproj")
        assert sf.path.name == HISTORY_FILENAME
        assert sf.started_at is not None
        assert sf.last_modified > 0
        # extra carries byte offsets the iter_records loop relies on.
        assert "start_offset" in sf.extra
        assert "end_offset" in sf.extra


def test_discover_sessions_across_multiple_repos(tmp_path: Path):
    _make_aider_repo(tmp_path, "alpha", TWO_SESSION_MD)
    _make_aider_repo(tmp_path / "projects", "beta", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    sessions = list(src.discover_sessions())
    # 2 repos x 2 sessions each.
    assert len(sessions) == 4
    cwds = {s.cwd for s in sessions}
    assert str(tmp_path / "alpha") in cwds
    assert str(tmp_path / "projects" / "beta") in cwds


def test_discover_sessions_max_files_cap(tmp_path: Path):
    for n in range(5):
        _make_aider_repo(tmp_path, f"r{n}", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path], max_files=2)
    sessions = list(src.discover_sessions())
    # 2 history files capped × 2 sessions each = 4.
    assert len(sessions) == 4


def test_discover_sessions_skips_pruned_dirs(tmp_path: Path):
    _make_aider_repo(tmp_path / "node_modules", "ignored", TWO_SESSION_MD)
    _make_aider_repo(tmp_path, "kept", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    cwds = {s.cwd for s in src.discover_sessions()}
    assert cwds == {str(tmp_path / "kept")}


# ---------------------------------------------------------------------------
# Record iteration
# ---------------------------------------------------------------------------


def test_iter_records_user_and_assistant_groups(tmp_path: Path):
    _make_aider_repo(tmp_path, "iter", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    sessions = sorted(src.discover_sessions(), key=lambda s: s.started_at or 0)
    # First session: one multi-line user msg, then one multi-line assistant.
    s1 = sessions[0]
    records = [r for _, r in src.iter_records(s1)]
    assert len(records) == 2
    assert isinstance(records[0], UserRecord)
    assert records[0].content_kind == "string"
    assert "Fix any errors below" in (records[0].text or "")
    # blank user line (just ####) was preserved as an empty line in the group.
    assert "Running:" in (records[0].text or "")

    assert isinstance(records[1], AssistantRecord)
    assert records[1].model is None  # information-poor: no model
    assert records[1].usage is None
    joined = "\n".join(b.text or "" for b in records[1].content)
    assert "flake8" in joined
    assert "E501" in joined


def test_iter_records_handles_freeform_block_inside_turn(tmp_path: Path):
    _make_aider_repo(tmp_path, "diff", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    sessions = sorted(src.discover_sessions(), key=lambda s: s.started_at or 0)
    s2 = sessions[1]
    records = [r for _, r in src.iter_records(s2)]
    assert len(records) == 2
    user, assistant = records
    assert isinstance(user, UserRecord)
    assert "Add a unit test" in (user.text or "")
    assert "empty-input case" in (user.text or "")
    assert isinstance(assistant, AssistantRecord)
    body = "\n".join(b.text or "" for b in assistant.content)
    # The fenced diff (no > prefix) should attach to the assistant turn.
    assert "def test_helper_empty" in body
    assert "Let me know" in body


def test_iter_records_offsets_are_monotonic(tmp_path: Path):
    _make_aider_repo(tmp_path, "mono", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    s1 = sorted(src.discover_sessions(), key=lambda s: s.started_at or 0)[0]
    offsets = [off for off, _ in src.iter_records(s1)]
    assert offsets == sorted(offsets)
    assert offsets[0] >= 1


def test_iter_records_respects_start_offset(tmp_path: Path):
    _make_aider_repo(tmp_path, "resume", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    s1 = sorted(src.discover_sessions(), key=lambda s: s.started_at or 0)[0]
    full = list(src.iter_records(s1))
    assert len(full) == 2
    tail = list(src.iter_records(s1, start_offset=full[0][0]))
    assert len(tail) == 1
    assert isinstance(tail[0][1], AssistantRecord)


def test_iter_records_empty_when_section_empty(tmp_path: Path):
    md = (
        "# aider chat started at 2025-01-15 14:23:01\n"
        "\n"
        "# aider chat started at 2025-01-16 09:00:00\n"
        "#### hi\n"
    )
    _make_aider_repo(tmp_path, "ev", md)
    src = AiderSource(search_roots=[tmp_path])
    sessions = sorted(src.discover_sessions(), key=lambda s: s.started_at or 0)
    empty_records = list(src.iter_records(sessions[0]))
    assert empty_records == []
    populated = list(src.iter_records(sessions[1]))
    assert len(populated) == 1
    assert isinstance(populated[0][1], UserRecord)


# ---------------------------------------------------------------------------
# Pathology
# ---------------------------------------------------------------------------


def test_pathological_no_session_headers(tmp_path: Path):
    _make_aider_repo(tmp_path, "weird", "#### only orphan\n> only orphan reply\n")
    src = AiderSource(search_roots=[tmp_path])
    # is_available finds the file (it exists)…
    assert src.is_available() is True
    # …but discover yields zero sessions because no header anchored them.
    assert list(src.discover_sessions()) == []


def test_pathological_garbage_header_timestamp(tmp_path: Path):
    md = "# aider chat started at not-a-timestamp\n#### body\n"
    _make_aider_repo(tmp_path, "bad-ts", md)
    src = AiderSource(search_roots=[tmp_path])
    assert list(src.discover_sessions()) == []


def test_iter_records_caches_findings(tmp_path: Path):
    """Second discover call should not re-walk the tree."""
    _make_aider_repo(tmp_path, "cache", TWO_SESSION_MD)
    src = AiderSource(search_roots=[tmp_path])
    first = list(src.discover_sessions())
    # Add another repo AFTER first discovery — cache means we ignore it.
    _make_aider_repo(tmp_path, "added-later", TWO_SESSION_MD)
    second = list(src.discover_sessions())
    assert len(first) == len(second)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registered_in_sources_list():
    from lib.sources.base import SOURCES

    assert AiderSource in SOURCES
