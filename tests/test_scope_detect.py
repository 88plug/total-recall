"""Tests for ``hooks.lib.scope_detect``.

Covers the mid-session scope-pivot detector. The detector is tuned for
high precision over recall — a false positive forces an unwanted
operator-context cache write, so we exercise the negative cases as
carefully as the positives.

Since scope_detect is now fully data-driven (derives keyword table from
the live DB), tests that require keyword-based scope scoring use monkeypatching
to inject a deterministic keyword table. Tests of infer_scope verify the
pure-string fallback behavior (returns cwd basename when DB is absent).
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# Make ``hooks.lib`` importable regardless of how pytest is invoked.
_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import hooks.lib.scope_detect as _scope_detect_mod  # noqa: E402
from hooks.lib.scope_detect import (  # noqa: E402
    PIVOT_REGEX,
    ScopeShift,
    detect_scope_shift,
    dominant_scope,
    infer_scope,
    score_scopes,
)

# ---------------------------------------------------------------------------
# Helpers — inject a deterministic scope-keyword table for keyword tests
# ---------------------------------------------------------------------------

# Generic test-scopes used throughout these tests (no author-specific names).
_TEST_KEYWORDS: dict[str, list[str]] = {
    "nova-api": ["nova-api", "nova", "api", "restapi", "endpoint"],
    "relay-fleet": ["relay-fleet", "relay", "fleet", "wireguard", "vpn"],
    "mail-server": ["mail-server", "mail", "postfix", "dovecot", "smtp", "imap"],
    "data-mine": ["data-mine", "data", "mine", "session", "transcript"],
}


@pytest.fixture(autouse=True)
def inject_test_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a deterministic keyword table for all tests in this file.

    Without this, score_scopes() returns {} (empty DB) and all keyword-based
    tests fail. This mirrors what the real DB-driven table would supply.
    """
    monkeypatch.setattr(_scope_detect_mod, "_SCOPE_KEYWORDS_CACHE", _TEST_KEYWORDS)


# ---------------------------------------------------------------------------
# infer_scope
# ---------------------------------------------------------------------------

def test_infer_scope_empty_returns_none():
    assert infer_scope("") is None
    assert infer_scope("/") is None


def test_infer_scope_fallback_returns_basename():
    """Without a DB, infer_scope falls back to the cwd basename."""
    assert infer_scope("/home/dana/nova-api") == "nova-api"
    assert infer_scope("/home/dana/relay-fleet") == "relay-fleet"
    assert infer_scope("/opt/some-tool") == "some-tool"


def test_infer_scope_deep_path_returns_last_segment():
    assert infer_scope("/var/lib/my-project/subdir") == "subdir"


# ---------------------------------------------------------------------------
# score_scopes
# ---------------------------------------------------------------------------

def test_score_scopes_counts_hits_per_scope():
    s = score_scopes("we need to fix the relay wireguard tunnel on the fleet")
    assert s["relay-fleet"] >= 3  # "relay", "wireguard", "fleet"
    assert s.get("nova-api", 0) == 0


def test_score_scopes_empty_text():
    s = score_scopes("")
    # All scopes get 0 hits.
    assert all(v == 0 for v in s.values())


# ---------------------------------------------------------------------------
# detect_scope_shift — positives
# ---------------------------------------------------------------------------

def test_cwd_change_is_strong_signal():
    shift = detect_scope_shift(
        current_prompt="ok continue",
        recent_prompts=[],
        current_cwd="/home/dana/mail-server",
        last_cwd="/home/dana/nova-api",
    )
    assert isinstance(shift, ScopeShift)
    assert shift.reason == "cwd"
    assert shift.new == "mail-server"
    assert shift.old == "nova-api"
    assert shift.confidence == 1.0


def test_keyword_only_positive_above_threshold():
    """Multiple keywords for a different scope than the recent context."""
    recent = [
        "fix the nova api endpoint",
        "restart the nova-api service",
        "check nova api logs",
    ]
    shift = detect_scope_shift(
        current_prompt="set up postfix and dovecot on the mail server with smtp",
        recent_prompts=recent,
        current_cwd="/home/dana/nova-api",
        last_cwd="/home/dana/nova-api",
    )
    assert shift is not None
    assert shift.new == "mail-server"
    assert shift.old == "nova-api"
    assert shift.reason == "keyword"
    assert shift.confidence >= 0.7


def test_keyword_plus_pivot_positive():
    recent = ["fix the relay tunnel", "check relay-fleet status"]
    shift = detect_scope_shift(
        current_prompt="now let's work on the data-mine transcript session",
        recent_prompts=recent,
        current_cwd="/home/dana/relay-fleet",
        last_cwd="/home/dana/relay-fleet",
    )
    assert shift is not None
    assert shift.reason == "keyword+pivot"
    assert shift.new == "data-mine"
    assert shift.confidence >= 0.9


# ---------------------------------------------------------------------------
# detect_scope_shift — negatives
# ---------------------------------------------------------------------------

def test_same_scope_no_shift():
    recent = ["fix the relay tunnel", "check relay-fleet wireguard"]
    shift = detect_scope_shift(
        current_prompt="restart the relay wireguard vpn on the fleet",
        recent_prompts=recent,
        current_cwd="/home/dana/relay-fleet",
        last_cwd="/home/dana/relay-fleet",
    )
    assert shift is None


def test_no_keyword_no_shift():
    shift = detect_scope_shift(
        current_prompt="ok thanks, looks good",
        recent_prompts=["fix the relay tunnel"],
        current_cwd="/home/dana/relay-fleet",
        last_cwd="/home/dana/relay-fleet",
    )
    assert shift is None


def test_low_confidence_single_keyword_no_pivot_no_recent():
    # 1 keyword, no pivot phrase, no recent context — confidence = 0.6 + 0 + 0.1 = 0.7
    shift = detect_scope_shift(
        current_prompt="check the postfix config",
        recent_prompts=[],
        current_cwd="/home/dana/nova-api",
        last_cwd="/home/dana/nova-api",
    )
    assert shift is not None
    assert shift.confidence == pytest.approx(0.7)
    assert shift.reason == "keyword"


def test_pivot_phrase_only_no_keywords_returns_none():
    # "now let's look at" is a pivot phrase, but there are no scope keywords.
    shift = detect_scope_shift(
        current_prompt="now let's look at this carefully",
        recent_prompts=["fix the relay tunnel"],
        current_cwd="/home/dana/relay-fleet",
        last_cwd="/home/dana/relay-fleet",
    )
    assert shift is None


def test_empty_prompts_list_with_keywords_still_works():
    # No recent context — dominant_scope returns None, so any top scope counts.
    shift = detect_scope_shift(
        current_prompt="now switch to data-mine transcript session",
        recent_prompts=[],
        current_cwd="/home/dana/nova-api",
        last_cwd="/home/dana/nova-api",
    )
    assert shift is not None
    assert shift.new == "data-mine"
    assert shift.old is None


def test_unknown_cwd_change_falls_back_to_path_string():
    shift = detect_scope_shift(
        current_prompt="continue",
        recent_prompts=[],
        current_cwd="/opt/weird/place",
        last_cwd="/home/dana/nova-api",
    )
    assert shift is not None
    assert shift.reason == "cwd"
    # Unknown cwd → raw path basename
    assert shift.new == "place"
    assert shift.old == "nova-api"


def test_mixed_scope_prompt_top_wins():
    # Prompt mentions relay-fleet (1 hit: "relay") and data-mine (2 hits: "data", "session").
    # Recent context is relay-fleet — top should be data-mine.
    recent = ["fix the relay tunnel", "check relay-fleet wireguard"]
    shift = detect_scope_shift(
        current_prompt="data session transcript from the relay run",
        recent_prompts=recent,
        current_cwd="/home/dana/relay-fleet",
        last_cwd="/home/dana/relay-fleet",
    )
    assert shift is not None
    assert shift.new == "data-mine"


# ---------------------------------------------------------------------------
# dominant_scope
# ---------------------------------------------------------------------------

def test_dominant_scope_empty():
    assert dominant_scope([]) is None


def test_dominant_scope_tiebreak_returns_some_scope():
    # Two scopes, each top in one prompt → tie. Result should still be one
    # of the tied scopes.
    prompts = ["relay wireguard fleet", "data session transcript"]
    result = dominant_scope(prompts)
    assert result in {"relay-fleet", "data-mine"}


def test_dominant_scope_majority():
    prompts = [
        "relay wireguard fleet",
        "relay tunnel fleet",
        "data session transcript",
    ]
    assert dominant_scope(prompts) == "relay-fleet"


# ---------------------------------------------------------------------------
# PIVOT_REGEX sanity (helper used internally)
# ---------------------------------------------------------------------------

def test_pivot_regex_matches_common_phrases():
    for phrase in [
        "now let's do this",
        "switch to mail server",
        "actually, pivot",
        "back to data-mine",
        "let's work on it",
        "move on to monitoring",
    ]:
        assert PIVOT_REGEX.search(phrase), f"should match: {phrase!r}"
