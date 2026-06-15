"""Tests for the voice-profile extractor + storage layer.

A small synthetic user-string corpus is fed through
:func:`extractors.voice_profile.measure_voice`, then persisted via
:func:`index.voice.persist_voice_profile`, and finally round-tripped
through :mod:`index.voice`'s query API.

Strategy:

* **Plausible-range** checks — not exact-value asserts. The extractor is
  a heuristic measurement, so we test *direction* (lowercase pct should
  be high when most turns are lowercase) rather than precise values that
  would brittle on every algorithm tweak.
* **Sample-size** is required to be the count of natural turns the
  extractor actually consumed — long pastes / XML / non-string content
  should be excluded.
* **Round-trip** via the SQLite layer asserts the JSON encode/decode
  preserves lists and floats.
"""

from __future__ import annotations

import sqlite3

import pytest

from extractors.voice_profile import (
    NATURAL_MAX_CHARS,
    measure_voice,
)
from index.voice import (
    VOICE_PROFILE_SCHEMA,
    ensure_schema,
    get_voice,
    get_voice_field,
    persist_voice_profile,
    upsert_voice_field,
)

# ---------------------------------------------------------------------------
# Helpers — build Record-like dicts cheap. We use plain dicts since
# :func:`measure_voice` accepts either ducktyped objects or raw JSONL
# shapes (it handles the message.content normalization itself).
# ---------------------------------------------------------------------------


def _user_string(text: str) -> dict:
    """A minimal user-string record with the fields the extractor reads."""
    return {
        "type": "user",
        "content_kind": "string",
        "text": text,
    }


def _user_string_obj(text: str):
    """RecordLike object — covers the attribute-access code path too."""

    class _Rec:
        pass

    r = _Rec()
    r.type = "user"
    r.content_kind = "string"
    r.text = text
    return r


def _user_list(text: str) -> dict:
    """A user record whose content is a list (should be skipped)."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "content_kind": "string",
        "text": text,
    }


@pytest.fixture
def fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(VOICE_PROFILE_SCHEMA)
    return conn


@pytest.fixture
def synth_corpus() -> list[dict]:
    """A synthetic corpus mimicking the measured operator voice.

    Most turns are lowercase, short, with no terminal punctuation. Some
    end with "?" (questions), one is "Yes" (capitalized one-word reply),
    a couple contain profanity, one carries a signature typo, several
    open with imperatives.
    """
    return [
        _user_string("check the shell"),
        _user_string("do it"),
        _user_string("yes"),
        _user_string("..."),
        _user_string("we should bounce the agent"),
        _user_string("fix the dns it's broken"),
        _user_string("run the test suite"),
        _user_string("what about ipv6?"),
        _user_string("nope - still borken !"),
        _user_string("wtf you broke dns"),
        _user_string("we dont need a turnstile do we"),
        _user_string("liek the other config"),
        _user_string("our setup uses vultr"),
        # Extra occurrences so borken/liek reach the _TYPO_MIN_FREQ=2 threshold.
        _user_string("the relay is borken again"),
        _user_string("liek this one not that"),
        _user_string("research deeply, use 5 agents"),
        _user_string("Yes"),
        _user_string("done"),
        _user_string("continue"),
        _user_string("check is all again"),
        _user_string("do all that"),
        _user_string("we are connected to both"),
        # An attribute-access object — exercises that code path.
        _user_string_obj("alread done, move on"),
        # Should be skipped: assistant turn.
        _assistant("On it — restarting unbound on 192.168.50.47."),
        # Should be skipped: list-content user record.
        _user_list("This came in as a structured list."),
        # Should be skipped: starts with "<" (system notification).
        _user_string("<system-reminder>do not echo</system-reminder>"),
        # Should be skipped: too long (>= NATURAL_MAX_CHARS).
        _user_string("x" * (NATURAL_MAX_CHARS + 10)),
    ]


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


def test_ensure_schema_idempotent(fresh_conn):
    ensure_schema(fresh_conn)
    ensure_schema(fresh_conn)
    row = fresh_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='voice_profile'"
    ).fetchone()
    assert row is not None


def test_upsert_and_get_field(fresh_conn):
    upsert_voice_field(
        fresh_conn,
        "lowercase_start_pct",
        0.795,
        sample_size=200,
    )
    got = get_voice_field(fresh_conn, "lowercase_start_pct")
    assert got is not None
    value, measured_at, sample_size = got
    assert value == pytest.approx(0.795)
    assert sample_size == 200
    assert isinstance(measured_at, int) and measured_at > 0


def test_upsert_overwrites_existing(fresh_conn):
    upsert_voice_field(fresh_conn, "mean_chars", 50)
    upsert_voice_field(fresh_conn, "mean_chars", 71)
    got = get_voice_field(fresh_conn, "mean_chars")
    assert got is not None
    assert got[0] == 71


def test_upsert_lists_and_dicts_round_trip(fresh_conn):
    upsert_voice_field(
        fresh_conn,
        "signature_typos",
        [["liek", 4], ["borken", 1]],
    )
    upsert_voice_field(
        fresh_conn,
        "top_first_words",
        [["check", 12], ["do", 9]],
    )
    profile = get_voice(fresh_conn)
    assert profile["signature_typos"] == [["liek", 4], ["borken", 1]]
    assert profile["top_first_words"] == [["check", 12], ["do", 9]]


def test_get_voice_field_missing_returns_none(fresh_conn):
    assert get_voice_field(fresh_conn, "nope_not_here") is None


def test_get_voice_empty_table_is_stable_shape(fresh_conn):
    profile = get_voice(fresh_conn)
    assert profile == {"_measured_at": {}, "_sample_size": {}}


def test_get_voice_exposes_voice_metadata(fresh_conn):
    upsert_voice_field(fresh_conn, "mean_chars", 71, sample_size=200)
    profile = get_voice(fresh_conn)
    assert "_measured_at" in profile and profile["_measured_at"]["mean_chars"] > 0
    assert profile["_sample_size"]["mean_chars"] == 200


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_measure_voice_empty_corpus_returns_zero_shape():
    profile = measure_voice([])
    assert profile["sample_size"] == 0
    assert profile["lowercase_start_pct"] == 0.0
    assert profile["mean_chars"] == 0
    assert profile["signature_typos"] == []
    assert profile["top_first_words"] == []


def test_measure_voice_skips_non_user_string_records(synth_corpus):
    profile = measure_voice(synth_corpus)
    # Hand-count: 23 user-string entries qualify (the last 4 in the
    # corpus are explicitly excluded — assistant, list-content,
    # XML-tag-leading, and the over-length blob).
    assert profile["sample_size"] == 23


def test_measure_voice_lowercase_majority(synth_corpus):
    profile = measure_voice(synth_corpus)
    # Most natural turns in the synthetic corpus are lowercase; the
    # measured pct should reflect that (>= 80%).
    assert profile["lowercase_start_pct"] >= 0.8
    assert profile["lowercase_start_pct"] <= 1.0


def test_measure_voice_length_distribution_in_plausible_range(synth_corpus):
    profile = measure_voice(synth_corpus)
    # Median natural turn in the synth corpus is short — well under 50
    # chars — and the max is bounded by NATURAL_MAX_CHARS.
    assert 1 <= profile["chars_p50"] <= 50
    assert profile["chars_p10"] <= profile["chars_p50"] <= profile["chars_p90"]
    assert profile["tokens_p10"] <= profile["tokens_p50"] <= profile["tokens_p90"]
    assert profile["mean_chars"] > 0
    assert profile["mean_tokens"] > 0


def test_measure_voice_punctuation_fractions_in_unit_interval(synth_corpus):
    profile = measure_voice(synth_corpus)
    for key in ("ends_period_pct", "ends_question_pct", "ends_no_punct_pct"):
        v = profile[key]
        assert 0.0 <= v <= 1.0, f"{key}={v} out of [0,1]"
    # The synth corpus has at least one question-mark ending.
    assert profile["ends_question_pct"] > 0.0
    # Most synth turns have no terminal punctuation.
    assert profile["ends_no_punct_pct"] >= 0.5


def test_measure_voice_imperative_density(synth_corpus):
    profile = measure_voice(synth_corpus)
    # The synth corpus opens with `check`, `do`, `fix`, `run`,
    # `research`, `continue` — comfortably above 10%.
    assert profile["imperative_first_word_pct"] >= 0.1
    assert profile["imperative_first_word_pct"] <= 1.0


def test_measure_voice_top_first_words_has_expected_entries(synth_corpus):
    profile = measure_voice(synth_corpus)
    words = {w for w, _ in profile["top_first_words"]}
    # At least one of these openers should appear at the top of the list.
    assert words & {"check", "do", "we", "yes", "research"}


def test_measure_voice_signature_typos_captured(synth_corpus):
    profile = measure_voice(synth_corpus)
    typo_map = dict(profile["signature_typos"])
    # `borken` and `liek` each appear twice in the synth corpus so they meet
    # the _TYPO_MIN_FREQ=2 threshold and should be promoted by _learn_typos.
    assert typo_map.get("borken", 0) >= 2
    assert typo_map.get("liek", 0) >= 2


def test_measure_voice_we_vs_i_ratio_skews_collective(synth_corpus):
    profile = measure_voice(synth_corpus)
    # The synth corpus has multiple "we"/"our"/"us" hits and zero
    # standalone "i" — ratio should be a positive count (because the
    # function returns `we_total` when i_total is 0).
    assert profile["we_vs_i_ratio"] > 0.0


def test_measure_voice_profanity_rate_nonzero(synth_corpus):
    profile = measure_voice(synth_corpus)
    # Synth corpus contains a `wtf` turn.
    assert profile["profanity_per_1k_turns"] > 0.0


def test_measure_voice_one_word_turn_pct(synth_corpus):
    profile = measure_voice(synth_corpus)
    # Synth corpus includes "yes", "...", "Yes", "done", "continue".
    assert profile["one_word_turn_pct"] >= 0.1
    assert profile["one_word_turn_pct"] <= 1.0


# ---------------------------------------------------------------------------
# End-to-end: measure → persist → reload
# ---------------------------------------------------------------------------


def test_round_trip_through_db(fresh_conn, synth_corpus):
    profile = measure_voice(synth_corpus)
    sample = profile.pop("sample_size")
    persist_voice_profile(fresh_conn, profile, sample_size=sample)

    loaded = get_voice(fresh_conn)

    # Every measured field round-tripped.
    for key, value in profile.items():
        # Stored value can come back as a list-of-lists (JSON tuples) so
        # convert any tuples in the original to lists for comparison.
        if isinstance(value, list):
            normalized = [list(x) if isinstance(x, tuple) else x for x in value]
            assert loaded[key] == normalized, f"mismatch on {key}"
        else:
            assert loaded[key] == value, f"mismatch on {key}"

    # Voice metadata is populated.
    assert loaded["_sample_size"]["mean_chars"] == sample
    assert loaded["_measured_at"]["mean_chars"] > 0


def test_persist_voice_profile_skips_reserved_keys(fresh_conn):
    # If a caller round-trips get_voice() back through
    # persist_voice_profile, the reserved `_measured_at` / `_sample_size`
    # reserved metadata dicts must not pollute the table.
    persist_voice_profile(
        fresh_conn,
        {
            "mean_chars": 71,
            "_measured_at": {"mean_chars": 12345},
            "_sample_size": {"mean_chars": 200},
        },
        sample_size=200,
    )
    rows = fresh_conn.execute(
        "SELECT key FROM voice_profile ORDER BY key"
    ).fetchall()
    keys = [r[0] for r in rows]
    assert keys == ["mean_chars"]
