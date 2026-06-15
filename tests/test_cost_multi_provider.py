"""Multi-provider tests for ``total_recall.cost``.

Complements ``test_cost.py`` (Anthropic-only) by exercising OpenAI and Google
model paths through ``detect_provider``, ``resolve_rate`` and
``estimate_cost``. The existing Anthropic tests must continue to pass —
verified by importing the same module and re-asserting two key invariants.
"""

from __future__ import annotations

import math

import pytest

from total_recall import cost as C

# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("claude-sonnet-4-6", "anthropic"),
    ("claude-opus-4-7", "anthropic"),
    ("sonnet-3-5", "anthropic"),
    ("gpt-5", "openai"),
    ("gpt-5-mini", "openai"),
    ("gpt-5-nano", "openai"),
    ("o3", "openai"),
    ("o4-mini", "openai"),
    ("gemini-2.5-pro", "google"),
    ("gemini-2.5-flash", "google"),
    ("gemini-pro", "google"),
    ("", "unknown"),
    ("some-random-llm", "unknown"),
])
def test_detect_provider(model, expected):
    assert C.detect_provider(model) == expected


# ---------------------------------------------------------------------------
# resolve_rate — OpenAI & Gemini
# ---------------------------------------------------------------------------

def test_resolve_rate_openai_gpt5():
    assert C.resolve_rate("gpt-5") == (1.25, 10.00)


def test_resolve_rate_openai_gpt5_mini():
    assert C.resolve_rate("gpt-5-mini") == (0.25, 2.00)


def test_resolve_rate_openai_gpt5_nano():
    assert C.resolve_rate("gpt-5-nano") == (0.05, 0.40)


def test_resolve_rate_openai_o3_o4():
    assert C.resolve_rate("o3") == (10.00, 40.00)
    assert C.resolve_rate("o4-mini") == (2.50, 10.00)


def test_resolve_rate_openai_family_fallback():
    # An unknown gpt-5-variant id falls through the longest-matching family
    # alias ("gpt-5") and inherits its rate.
    assert C.resolve_rate("gpt-5-future-2027") == C.DEFAULT_RATES["gpt-5"]


def test_resolve_rate_openai_family_mini_beats_base():
    # The longest-match rule means a "gpt-5-mini" variant matches mini, not
    # the broader gpt-5 family.
    assert C.resolve_rate("gpt-5-mini-2027") == C.DEFAULT_RATES["gpt-5-mini"]


def test_resolve_rate_gemini_pro():
    assert C.resolve_rate("gemini-2.5-pro") == (1.00, 10.00)


def test_resolve_rate_gemini_flash():
    assert C.resolve_rate("gemini-2.5-flash") == (0.30, 2.50)


def test_resolve_rate_gemini_flash_lite():
    assert C.resolve_rate("gemini-2.5-flash-lite") == (0.10, 0.40)


def test_resolve_rate_gemini_alias_pro():
    # Bare "gemini-pro" / older naming falls back to the alias entry.
    assert C.resolve_rate("gemini-pro") == (1.00, 10.00)


def test_resolve_rate_unknown_gemini_id_uses_default():
    # A future "gemini-3.0-pro" id won't substring-match any registered
    # family (the version number breaks "gemini-pro"). It falls through to
    # the global default — conservative behaviour, prefer a forced override
    # via TOTAL_RECALL_RATES_JSON rather than guessing the new tier.
    assert C.resolve_rate("gemini-3.0-pro") == C.DEFAULT_RATES["default"]
    # But a version-less "gemini-pro" alias still resolves cleanly.
    assert C.resolve_rate("custom-gemini-pro-id") == C.DEFAULT_RATES["gemini-pro"]


# ---------------------------------------------------------------------------
# estimate_cost — provider-specific cache behaviour
# ---------------------------------------------------------------------------

def test_estimate_cost_openai_gpt5_cached_input_at_10pct():
    # GPT-5 cached_input bills at 10% of $1.25 = $0.125 / Mtok.
    # 1M input + 500k cache_read on gpt-5:
    #   1M * 1.25 / 1e6  +  500k * 1.25 * 0.10 / 1e6
    #   = 1.25 + 0.0625 = 1.3125
    got = C.estimate_cost(
        model="gpt-5",
        input_tokens=1_000_000,
        cache_read_tokens=500_000,
    )
    expected = 1.25 + (500_000 * 1.25 * 0.10 / 1e6)
    assert math.isclose(got, expected, abs_tol=1e-9)


def test_estimate_cost_openai_cached_input_override_to_50pct():
    # Older GPT-4o-era pricing was 50% on cached_input. Operators stuck on
    # that family can pass `cache_multipliers={"read": 0.5}` and get the
    # higher number rather than the GPT-5 default.
    got = C.estimate_cost(
        model="gpt-5",
        cache_read_tokens=1_000_000,
        cache_multipliers={"read": 0.5},
    )
    # 1M * 1.25 * 0.5 / 1e6 = 0.625
    assert math.isclose(got, 0.625, abs_tol=1e-9)


def test_estimate_cost_openai_does_not_apply_anthropic_125pct_create():
    # OpenAI cache "create" multiplier is 1.0× (implicit / free at the API
    # level — we just don't double-bill if a transcript happens to carry a
    # non-zero create count).
    got = C.estimate_cost(
        model="gpt-5",
        cache_creation_tokens=1_000_000,
    )
    # 1M * 1.25 * 1.0 / 1e6 = 1.25
    assert math.isclose(got, 1.25, abs_tol=1e-9)


def test_estimate_cost_gemini_pro_cache_read_at_10pct():
    # Gemini context caching reads at 10% of input rate ($1.00 / Mtok).
    got = C.estimate_cost(
        model="gemini-2.5-pro",
        cache_read_tokens=1_000_000,
    )
    # 1M * 1.00 * 0.10 / 1e6 = 0.10
    assert math.isclose(got, 0.10, abs_tol=1e-9)


def test_estimate_cost_gemini_combined():
    # 1M input + 1M output on Gemini 2.5 Flash = 0.30 + 2.50 = 2.80
    got = C.estimate_cost(
        model="gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert math.isclose(got, 2.80, abs_tol=1e-9)


def test_estimate_cost_openai_input_only():
    # 1M input on gpt-5-nano = $0.05
    got = C.estimate_cost(model="gpt-5-nano", input_tokens=1_000_000)
    assert math.isclose(got, 0.05, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Anthropic invariants still hold (spec requirement: "Existing tests still pass")
# ---------------------------------------------------------------------------

def test_estimate_cost_anthropic_sonnet_cache_read_unchanged():
    # The Anthropic 10% rule is unchanged from the original module.
    got = C.estimate_cost(cache_read_tokens=1_000_000, model="claude-sonnet-4-6")
    assert math.isclose(got, 0.30, abs_tol=1e-9)


def test_estimate_cost_anthropic_sonnet_cache_create_unchanged():
    # Anthropic 125% rule on cache creation.
    got = C.estimate_cost(cache_creation_tokens=1_000_000, model="claude-sonnet-4-6")
    assert math.isclose(got, 3.75, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Provider tables
# ---------------------------------------------------------------------------

def test_provider_cache_multipliers_present_for_all_three():
    for p in ("anthropic", "openai", "google"):
        assert p in C.PROVIDER_CACHE_MULTIPLIERS
        assert "read" in C.PROVIDER_CACHE_MULTIPLIERS[p]
        assert "create" in C.PROVIDER_CACHE_MULTIPLIERS[p]


def test_anthropic_cache_constants_back_compat():
    # The module-level CACHE_READ_MULTIPLIER / CACHE_WRITE_MULTIPLIER names
    # are imported elsewhere; preserving them prevents silent breakage in
    # callers that haven't migrated to PROVIDER_CACHE_MULTIPLIERS yet.
    assert C.PROVIDER_CACHE_MULTIPLIERS["anthropic"]["read"] == C.CACHE_READ_MULTIPLIER
    assert C.PROVIDER_CACHE_MULTIPLIERS["anthropic"]["create"] == C.CACHE_WRITE_MULTIPLIER
