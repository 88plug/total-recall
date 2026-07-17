"""Tests for the implicit-preference extractor + storage layer.

Strategy:
- Synthetic corpora built from raw dicts (same shape the extractor accepts).
- Promotion thresholds are exercised explicitly (4 sessions → NOT promoted,
  5+ sessions → promoted).
- No operator-specific literals — all values are derived from test inputs.
- Storage round-trip via an in-memory SQLite connection.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from extractors.implicit_preferences import (
    PROMOTION_MIN_SESSIONS,
    ImplicitPreference,
    ImplicitPreferenceProfile,
    extract_implicit_preferences,
)
from index.implicit_preferences import (
    ensure_schema,
    get_implicit_preferences,
    persist_implicit_preferences,
)

# ---------------------------------------------------------------------------
# Test record builders
# ---------------------------------------------------------------------------


def _tool_use_block(name: str, input_payload: dict | None = None) -> dict:
    return {
        "type": "tool_use",
        "id": f"tu_{name}",
        "name": name,
        "input": input_payload or {},
    }


def _assistant_with_tools(*tool_names: str, cmd: str = "") -> dict:
    """Assistant record that called the given tools."""
    blocks = []
    for name in tool_names:
        if name in ("bash", "shell", "run_bash"):
            inp = {"command": cmd} if cmd else {"command": ""}
        else:
            inp = {}
        blocks.append(_tool_use_block(name, inp))
    return {
        "type": "assistant",
        "sessionId": "",
        "cwd": "",
        "timestamp": time.time(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}] + blocks,
        },
    }


def _user_turn(text: str, session_id: str = "s1", cwd: str = "/proj/a") -> dict:
    return {
        "type": "user",
        "content_kind": "string",
        "text": text,
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": time.time(),
    }


def _make_session(
    session_id: str,
    cwd: str,
    n_edit: int = 0,
    n_write: int = 0,
    bash_cmds: list[str] | None = None,
    user_texts: list[str] | None = None,
) -> tuple[str, str, list[dict]]:
    """Build a (session_id, cwd, records) triple."""
    records: list[dict] = []

    # Set session_id and cwd on each record.
    def _set(rec: dict) -> dict:
        rec["sessionId"] = session_id
        rec["cwd"] = cwd
        return rec

    for _ in range(n_edit):
        records.append(_set(_assistant_with_tools("edit")))
    for _ in range(n_write):
        records.append(_set(_assistant_with_tools("write")))
    for cmd in bash_cmds or []:
        records.append(_set(_assistant_with_tools("bash", cmd=cmd)))
    for txt in user_texts or []:
        records.append(_set(_user_turn(txt, session_id=session_id, cwd=cwd)))

    return session_id, cwd, records


def _spread_timestamps(sessions: list[tuple], days: float = 14.0) -> list[tuple]:
    """Spread session timestamps across `days` so the stability check passes."""
    import time as _time

    spread = []
    now = _time.time()
    step = (days * 86400) / max(len(sessions), 1)
    for i, (sid, cwd, records) in enumerate(sessions):
        ts = now - (days * 86400) + i * step
        patched = []
        for rec in records:
            r = dict(rec)
            r["timestamp"] = ts + 1
            patched.append(r)
        spread.append((sid, cwd, patched))
    return spread


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Tests: Edit >> Write → edit_strategy=prefer_edit
# ---------------------------------------------------------------------------


class TestEditStrategy:
    def _edit_dominant_sessions(self, n: int, start_idx: int = 0) -> list[tuple]:
        """n sessions each with 10 Edit and 0 Write calls across 3+ projects."""
        projects = ["/proj/a", "/proj/b", "/proj/c", "/proj/d"]
        sessions = [
            _make_session(
                f"sess_{start_idx + i}",
                projects[i % len(projects)],
                n_edit=10,
                n_write=1,  # ratio = 10, well above 2×
            )
            for i in range(n)
        ]
        return _spread_timestamps(sessions)

    def test_edit_preferred_with_enough_sessions(self):
        sessions = self._edit_dominant_sessions(PROMOTION_MIN_SESSIONS + 2)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") in cats, (
            f"Expected edit_strategy=prefer_edit in {cats}"
        )
        pref = next(p for p in profile.preferences if p.category == "edit_strategy")
        assert pref.confidence > 0.6

    def test_edit_not_promoted_below_session_threshold(self):
        sessions = self._edit_dominant_sessions(PROMOTION_MIN_SESSIONS - 1)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") not in cats, (
            "Should NOT emit prefer_edit with only 4 sessions"
        )

    def test_no_edit_preference_when_balanced(self):
        sessions = [
            _make_session(f"sess_{i}", f"/proj/{i % 3}", n_edit=3, n_write=3) for i in range(8)
        ]
        sessions = _spread_timestamps(sessions)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") not in cats


# ---------------------------------------------------------------------------
# Tests: Heavy bash uv, zero pip → shell_command=prefer_uv
# ---------------------------------------------------------------------------


class TestShellCommands:
    def _uv_sessions(self, n: int) -> list[tuple]:
        projects = ["/proj/a", "/proj/b", "/proj/c", "/proj/d"]
        sessions = [
            _make_session(
                f"sess_{i}",
                projects[i % len(projects)],
                bash_cmds=["uv install requests"] * 3,
            )
            for i in range(n)
        ]
        return _spread_timestamps(sessions)

    def test_prefer_uv_with_enough_sessions(self):
        sessions = self._uv_sessions(PROMOTION_MIN_SESSIONS + 3)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("shell_command", "prefer_uv") in cats, f"Expected shell_command=prefer_uv in {cats}"

    def test_prefer_uv_not_promoted_below_threshold(self):
        sessions = self._uv_sessions(PROMOTION_MIN_SESSIONS - 1)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("shell_command", "prefer_uv") not in cats

    def test_no_preference_when_mixed_managers(self):
        """uv and pip used equally → no dominant tool."""
        projects = ["/proj/a", "/proj/b", "/proj/c"]
        sessions = []
        for i in range(10):
            cmd = "uv install" if i % 2 == 0 else "pip install"
            sessions.append(
                _make_session(
                    f"sess_{i}",
                    projects[i % len(projects)],
                    bash_cmds=[cmd],
                )
            )
        sessions = _spread_timestamps(sessions)
        profile = extract_implicit_preferences(sessions)
        cats = {p.value for p in profile.preferences if p.category == "shell_command"}
        assert "prefer_uv" not in cats
        assert "prefer_pip" not in cats


# ---------------------------------------------------------------------------
# Tests: Empty corpus → empty profile
# ---------------------------------------------------------------------------


class TestEmptyCorpus:
    def test_empty_iterable(self):
        profile = extract_implicit_preferences([])
        assert isinstance(profile, ImplicitPreferenceProfile)
        assert profile.preferences == []
        assert profile.sample_size == 0

    def test_sessions_with_no_tool_calls(self):
        sessions = [_make_session(f"s{i}", f"/proj/{i % 3}") for i in range(6)]
        sessions = _spread_timestamps(sessions)
        profile = extract_implicit_preferences(sessions)
        assert isinstance(profile, ImplicitPreferenceProfile)
        assert profile.sample_size == 6


# ---------------------------------------------------------------------------
# Tests: Promotion threshold boundary
# ---------------------------------------------------------------------------


class TestPromotionThreshold:
    def _minimal_edit_sessions(self, n: int) -> list[tuple]:
        """n sessions, all edit-dominant, spread across 3 projects, ≥7-day span."""
        projects = ["/proj/a", "/proj/b", "/proj/c"]
        sessions = [
            _make_session(
                f"sess_{i}",
                projects[i % len(projects)],
                n_edit=10,
                n_write=0,
            )
            for i in range(n)
        ]
        return _spread_timestamps(sessions, days=14.0)

    def test_exactly_4_sessions_not_promoted(self):
        sessions = self._minimal_edit_sessions(4)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") not in cats, (
            "4 sessions must NOT cross the promotion threshold"
        )

    def test_exactly_5_sessions_promoted(self):
        sessions = self._minimal_edit_sessions(5)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") in cats, (
            "5 sessions across 3 projects over 14 days MUST be promoted"
        )

    def test_projects_threshold_not_met(self):
        """5 sessions but only 2 distinct projects → NOT promoted."""
        sessions = [
            _make_session(f"sess_{i}", "/proj/only_two" if i < 4 else "/proj/two", n_edit=10)
            for i in range(5)
        ]
        sessions = _spread_timestamps(sessions, days=14.0)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") not in cats, (
            "Only 2 projects must NOT cross project threshold"
        )

    def test_stability_not_met(self):
        """5 sessions, 3 projects, but all within 3 days → NOT promoted."""
        sessions = [
            _make_session(
                f"sess_{i}",
                ["/proj/a", "/proj/b", "/proj/c"][i % 3],
                n_edit=10,
            )
            for i in range(5)
        ]
        # 3-day spread, below the 7-day minimum.
        sessions = _spread_timestamps(sessions, days=3.0)
        profile = extract_implicit_preferences(sessions)
        cats = {(p.category, p.value) for p in profile.preferences}
        assert ("edit_strategy", "prefer_edit") not in cats, (
            "3-day span must NOT pass the stability check"
        )

    def test_is_promoted_method_directly(self):
        pref = ImplicitPreference(
            category="edit_strategy",
            value="prefer_edit",
            confidence=0.9,
            evidence_sessions=5,
            evidence_projects=3,
            contradiction_count=0,
            sample_phrases=[],
        )
        now = time.time()
        # 14-day spread → should promote.
        assert pref.is_promoted(
            first_seen_ts=now - 14 * 86400,
            last_seen_ts=now,
        )
        # 3-day spread → should not.
        assert not pref.is_promoted(
            first_seen_ts=now - 3 * 86400,
            last_seen_ts=now,
        )

    def test_contradiction_ratio_blocks_promotion(self):
        """If contradiction ratio < 0.80, preference must not be promoted."""
        pref = ImplicitPreference(
            category="edit_strategy",
            value="prefer_edit",
            confidence=0.7,
            evidence_sessions=5,
            evidence_projects=3,
            contradiction_count=2,  # 5/(5+2) ≈ 0.71 < 0.80
            sample_phrases=[],
        )
        now = time.time()
        assert not pref.is_promoted(
            first_seen_ts=now - 14 * 86400,
            last_seen_ts=now,
        )

    def test_contradiction_ratio_allows_promotion_at_80_pct(self):
        """5 evidence, 1 contradiction → 5/6 ≈ 0.833 ≥ 0.80 → promoted."""
        pref = ImplicitPreference(
            category="edit_strategy",
            value="prefer_edit",
            confidence=0.85,
            evidence_sessions=5,
            evidence_projects=3,
            contradiction_count=1,
        )
        now = time.time()
        assert pref.is_promoted(
            first_seen_ts=now - 14 * 86400,
            last_seen_ts=now,
        )


# ---------------------------------------------------------------------------
# Tests: Storage round-trip
# ---------------------------------------------------------------------------


class TestStorageRoundTrip:
    def _simple_profile(self) -> ImplicitPreferenceProfile:
        pref = ImplicitPreference(
            category="edit_strategy",
            value="prefer_edit",
            confidence=0.92,
            evidence_sessions=10,
            evidence_projects=4,
            contradiction_count=1,
            sample_phrases=["fix the file", "edit this"],
        )
        return ImplicitPreferenceProfile(preferences=[pref], sample_size=10)

    def test_persist_and_retrieve(self, mem_conn):
        profile = self._simple_profile()
        persist_implicit_preferences(mem_conn, profile, now=1700000000)
        rows = get_implicit_preferences(mem_conn, min_confidence=0.0)
        assert len(rows) == 1
        row = rows[0]
        assert row["category"] == "edit_strategy"
        assert row["value"] == "prefer_edit"
        assert abs(row["confidence"] - 0.92) < 1e-4
        assert row["evidence_sessions"] == 10
        assert row["evidence_projects"] == 4
        assert row["contradiction_count"] == 1
        assert isinstance(row["sample_phrases"], list)
        assert "fix the file" in row["sample_phrases"]

    def test_min_confidence_filter(self, mem_conn):
        prefs = [
            ImplicitPreference("a", "high", 0.9, 10, 3, 0, []),
            ImplicitPreference("b", "low", 0.3, 5, 3, 0, []),
        ]
        profile = ImplicitPreferenceProfile(preferences=prefs, sample_size=10)
        persist_implicit_preferences(mem_conn, profile)
        rows = get_implicit_preferences(mem_conn, min_confidence=0.6)
        assert len(rows) == 1
        assert rows[0]["category"] == "a"

    def test_upsert_preserves_first_seen_ts(self, mem_conn):
        """first_seen_ts must not be overwritten on a second upsert."""
        pref = ImplicitPreference("edit_strategy", "prefer_edit", 0.9, 5, 3, 0, [])
        profile = ImplicitPreferenceProfile(preferences=[pref], sample_size=5)
        persist_implicit_preferences(mem_conn, profile, now=1000)
        # Second upsert with a later timestamp.
        persist_implicit_preferences(mem_conn, profile, now=2000)
        rows = get_implicit_preferences(mem_conn, min_confidence=0.0)
        assert rows[0]["first_seen_ts"] == 1000
        assert rows[0]["last_seen_ts"] == 2000

    def test_empty_profile_no_rows(self, mem_conn):
        profile = ImplicitPreferenceProfile(preferences=[], sample_size=0)
        persist_implicit_preferences(mem_conn, profile)
        rows = get_implicit_preferences(mem_conn, min_confidence=0.0)
        assert rows == []

    def test_category_filter(self, mem_conn):
        prefs = [
            ImplicitPreference("edit_strategy", "prefer_edit", 0.9, 10, 3, 0, []),
            ImplicitPreference("format", "no_emojis_in_chat", 0.95, 12, 4, 0, []),
        ]
        profile = ImplicitPreferenceProfile(preferences=prefs, sample_size=12)
        persist_implicit_preferences(mem_conn, profile)
        rows = get_implicit_preferences(mem_conn, min_confidence=0.0, category="format")
        assert len(rows) == 1
        assert rows[0]["value"] == "no_emojis_in_chat"


# ---------------------------------------------------------------------------
# Tests: No hardcoded operator-specific literals
# ---------------------------------------------------------------------------


class TestNoHardcodedValues:
    """Ensure the extractor emits values derived from input, not from hardcoded lists."""

    def test_detected_command_comes_from_input(self):
        """The emitted shell_command preference value must match the actual command used."""
        # Use a made-up command name that can't possibly be hardcoded.
        fake_cmd = "myspecialtool"
        # We need to ensure it's in a command group; add it dynamically for this test.
        from extractors.implicit_preferences import _CMD_TO_GROUP, _COMMAND_GROUPS

        # Inject a temporary group.
        fake_group = [fake_cmd]
        fake_gi = len(_COMMAND_GROUPS)
        _COMMAND_GROUPS.append(fake_group)
        _CMD_TO_GROUP[fake_cmd] = fake_gi

        try:
            projects = ["/proj/a", "/proj/b", "/proj/c", "/proj/d"]
            sessions = [
                _make_session(
                    f"sess_{i}",
                    projects[i % len(projects)],
                    bash_cmds=[f"{fake_cmd} run"],
                )
                for i in range(7)
            ]
            sessions = _spread_timestamps(sessions)
            profile = extract_implicit_preferences(sessions)
            cats = {p.value for p in profile.preferences if p.category == "shell_command"}
            assert f"prefer_{fake_cmd}" in cats, (
                f"Expected prefer_{fake_cmd} derived from input corpus, got {cats}"
            )
        finally:
            # Clean up the temporary injection.
            _COMMAND_GROUPS.pop()
            _CMD_TO_GROUP.pop(fake_cmd, None)
