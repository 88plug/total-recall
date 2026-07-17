"""Tests for total_recall.cost — price catalog + cost estimation."""

from __future__ import annotations

import json
import math

import pytest

from total_recall import cost as C

# ---------------------------------------------------------------------------
# resolve_rate
# ---------------------------------------------------------------------------


def test_resolve_rate_exact_default():
    assert C.resolve_rate("claude-sonnet-4-6") == (3.00, 15.00)
    assert C.resolve_rate("claude-opus-4-7") == (15.00, 75.00)


def test_resolve_rate_family_alias_sonnet_variant():
    # Both an exact-default name and an unknown-but-family name resolve to sonnet rates.
    sonnet_rate = C.DEFAULT_RATES["sonnet"]
    assert C.resolve_rate("claude-sonnet-4-6") == sonnet_rate
    assert C.resolve_rate("sonnet-3-5") == sonnet_rate
    assert C.resolve_rate("claude-3-5-sonnet-20240620") == sonnet_rate


def test_resolve_rate_family_alias_haiku_opus():
    assert C.resolve_rate("some-unknown-haiku-id") == C.DEFAULT_RATES["haiku"]
    assert C.resolve_rate("future-opus-vNext") == C.DEFAULT_RATES["opus"]


def test_resolve_rate_exact_beats_family():
    # Caller-supplied exact match wins over the default family fallback.
    override = {"claude-sonnet-4-6": (1.23, 4.56)}
    assert C.resolve_rate("claude-sonnet-4-6", rates=override) == (1.23, 4.56)
    # A different sonnet id with no exact override falls through to family default.
    assert C.resolve_rate("sonnet-3-5", rates=override) == C.DEFAULT_RATES["sonnet"]


def test_resolve_rate_case_insensitive():
    assert C.resolve_rate("Claude-Sonnet-4-6") == C.DEFAULT_RATES["claude-sonnet-4-6"]
    assert C.resolve_rate("HAIKU") == C.DEFAULT_RATES["haiku"]


def test_resolve_rate_unknown_returns_default():
    assert C.resolve_rate("gpt-4o-mini") == C.DEFAULT_RATES["default"]
    assert C.resolve_rate("") == C.DEFAULT_RATES["default"]


def test_resolve_rate_caller_family_override(monkeypatch):
    # Caller supplies a custom 'sonnet' family — non-exact sonnet model uses it.
    override = {"sonnet": (9.0, 90.0)}
    assert C.resolve_rate("brand-new-sonnet-id", rates=override) == (9.0, 90.0)


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_input_only_sonnet():
    # 1M input tokens at sonnet ($3/Mtok in) = $3.00
    got = C.estimate_cost(input_tokens=1_000_000, model="claude-sonnet-4-6")
    assert math.isclose(got, 3.00, abs_tol=1e-9)


def test_estimate_cost_output_only_sonnet():
    got = C.estimate_cost(output_tokens=1_000_000, model="claude-sonnet-4-6")
    assert math.isclose(got, 15.00, abs_tol=1e-9)


def test_estimate_cost_cache_read_is_10pct_of_input():
    # 1M cache-read tokens at sonnet input $3 * 10% = $0.30
    got = C.estimate_cost(cache_read_tokens=1_000_000, model="claude-sonnet-4-6")
    assert math.isclose(got, 0.30, abs_tol=1e-9)


def test_estimate_cost_cache_creation_is_125pct_of_input():
    # 1M cache-creation tokens at sonnet input $3 * 1.25 = $3.75
    got = C.estimate_cost(cache_creation_tokens=1_000_000, model="claude-sonnet-4-6")
    assert math.isclose(got, 3.75, abs_tol=1e-9)


def test_estimate_cost_combined():
    # Mixed: 100k input + 200k cache_read + 50k cache_create + 80k output @ sonnet
    got = C.estimate_cost(
        input_tokens=100_000,
        cache_read_tokens=200_000,
        cache_creation_tokens=50_000,
        output_tokens=80_000,
        model="claude-sonnet-4-6",
    )
    expected = (
        100_000 * 3.0 / 1e6
        + 200_000 * 3.0 * 0.10 / 1e6
        + 50_000 * 3.0 * 1.25 / 1e6
        + 80_000 * 15.0 / 1e6
    )
    assert math.isclose(got, expected, abs_tol=1e-9)


def test_estimate_cost_zero_tokens_is_zero():
    assert C.estimate_cost(model="claude-opus-4-7") == 0.0


def test_estimate_cost_unknown_model_uses_default():
    # default == sonnet rates ($3/$15)
    got = C.estimate_cost(input_tokens=1_000_000, model="unobtainium-9000")
    assert math.isclose(got, 3.00, abs_tol=1e-9)


def test_estimate_cost_with_rates_override():
    rates = {"opus": (10.0, 50.0)}
    got = C.estimate_cost(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        model="claude-opus-4-7",
        rates=rates,
    )
    # claude-opus-4-7 has exact default (15,75); caller rates only has "opus" family.
    # Exact default wins over caller's family.
    assert math.isclose(got, 15.0 + 75.0, abs_tol=1e-9)

    # But an unknown opus id should use the caller's family override.
    got2 = C.estimate_cost(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        model="claude-opus-vNext",
        rates=rates,
    )
    assert math.isclose(got2, 10.0 + 50.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# parse_rate_arg
# ---------------------------------------------------------------------------


def test_parse_rate_arg_basic():
    assert C.parse_rate_arg("sonnet=3/15") == ("sonnet", (3.0, 15.0))


def test_parse_rate_arg_decimals_and_whitespace():
    assert C.parse_rate_arg("  Opus = 15.5 / 75.25 ") == ("opus", (15.5, 75.25))


def test_parse_rate_arg_lowercases_name():
    name, _ = C.parse_rate_arg("CLAUDE-SONNET-4-6=3/15")
    assert name == "claude-sonnet-4-6"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "no-equals",
        "name=",
        "name=3",
        "name=3/",
        "name=/15",
        "name=abc/15",
        "name=3/xyz",
        "=3/15",
        "name=-1/15",
    ],
)
def test_parse_rate_arg_bad_inputs_raise(bad):
    with pytest.raises(ValueError):
        C.parse_rate_arg(bad)


def test_parse_rate_arg_non_string_raises():
    with pytest.raises(ValueError):
        C.parse_rate_arg(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load_env_rates
# ---------------------------------------------------------------------------


def test_load_env_rates_missing(monkeypatch):
    monkeypatch.delenv("TOTAL_RECALL_RATES_JSON", raising=False)
    assert C.load_env_rates() == {}


def test_load_env_rates_roundtrip(monkeypatch):
    payload = {"claude-sonnet-4-6": [2.5, 12.5], "opus": [20.0, 100.0]}
    monkeypatch.setenv("TOTAL_RECALL_RATES_JSON", json.dumps(payload))
    got = C.load_env_rates()
    assert got == {
        "claude-sonnet-4-6": (2.5, 12.5),
        "opus": (20.0, 100.0),
    }


def test_load_env_rates_invalid_json(monkeypatch):
    monkeypatch.setenv("TOTAL_RECALL_RATES_JSON", "{not-json")
    assert C.load_env_rates() == {}


def test_load_env_rates_non_object(monkeypatch):
    monkeypatch.setenv("TOTAL_RECALL_RATES_JSON", "[1, 2, 3]")
    assert C.load_env_rates() == {}


def test_load_env_rates_skips_bad_entries(monkeypatch):
    payload = {
        "ok": [1.0, 2.0],
        "wrong-shape": [1.0],
        "non-numeric": ["a", "b"],
        "good2": [3, 4],  # ints coerce to floats
    }
    monkeypatch.setenv("TOTAL_RECALL_RATES_JSON", json.dumps(payload))
    got = C.load_env_rates()
    assert got == {"ok": (1.0, 2.0), "good2": (3.0, 4.0)}
