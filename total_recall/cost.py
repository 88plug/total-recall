"""Multi-provider model price catalog and cost estimation helpers.

Prices are USD per million tokens (input, output). Cache behaviour differs by
provider — see :data:`PROVIDER_CACHE_MULTIPLIERS`:

* **Anthropic** — cache reads bill at 10% of input, cache *creation* at 125%.
* **OpenAI** — ``cached_input`` is billed by OpenAI directly (no separate
  create event); we treat it as a multiplier of input. The GPT-5 family (and
  newer) bill cached input at 10% of standard. Cache "create" is implicit
  and not separately billed.
* **Google Gemini** — context caching reads bill at 10% of standard input;
  cache storage is time-based and *not* modelled here (we don't see storage
  events in Claude Code transcripts).

These rates are checked-in defaults verified against live provider pricing
pages in May 2026. Users can override via:

* CLI: ``total-recall metrics cost --rate sonnet=3/15 --rate gpt-5=1.25/10``
* Env: ``TOTAL_RECALL_RATES_JSON='{"gpt-5": [1.25, 10.0]}'``
"""

from __future__ import annotations

import json
import os
from typing import Mapping

# ---------------------------------------------------------------------------
# Default per-Mtok rates (USD), verified May 2026.
# ---------------------------------------------------------------------------
#
# Sources: openai.com/api/pricing, ai.google.dev/gemini-api/docs/pricing,
# anthropic.com/pricing. Where the provider only publishes a single "tier"
# rate per model we use it as-is.

DEFAULT_RATES: dict[str, tuple[float, float]] = {
    # ---- Anthropic family aliases (used when exact model isn't matched) ----
    "haiku":  ( 0.80,  4.00),
    "sonnet": ( 3.00, 15.00),
    "opus":   (15.00, 75.00),
    # Specific Anthropic model ids
    "claude-haiku-4-5-20251001":  ( 1.00,  5.00),
    "claude-sonnet-4-6":          ( 3.00, 15.00),
    "claude-opus-4-7":            (15.00, 75.00),

    # ---- OpenAI (Codex CLI) ----
    # GPT-5 line: $1.25/$10 (released Aug 2025, still the cheap tier in 2026).
    # GPT-5.4 doubled to $2.50/$15, GPT-5.5 doubled again to $5/$30 — users
    # who need those must override via --rate or the env var.
    "gpt-5":      ( 1.25, 10.00),
    "gpt-5-mini": ( 0.25,  2.00),
    "gpt-5-nano": ( 0.05,  0.40),
    "o3":         (10.00, 40.00),
    "o4-mini":    ( 2.50, 10.00),

    # ---- Google Gemini (Gemini CLI) ----
    "gemini-2.5-pro":        ( 1.00, 10.00),
    "gemini-2.5-flash":      ( 0.30,  2.50),
    "gemini-2.5-flash-lite": ( 0.10,  0.40),
    # Family alias — bare 'gemini' or older 'gemini-pro' falls through to Pro rates.
    "gemini-pro":            ( 1.00, 10.00),

    # ---- Fallback ----
    "default": ( 3.00, 15.00),
}

# Per-provider cache pricing multipliers (relative to the input rate).
#
# * read   — applied to cache_read_tokens
# * create — applied to cache_creation_tokens (Anthropic only; for the other
#            providers cache creation is implicit and free, so we leave the
#            multiplier at 1.0 but the corresponding token-count field is
#            normally zero on those transcripts)
PROVIDER_CACHE_MULTIPLIERS: dict[str, dict[str, float]] = {
    "anthropic": {"read": 0.10, "create": 1.25},
    # OpenAI: GPT-5 family cached_input bills at 10% of standard input. Cache
    # creation is implicit (no separate "write" event) — we bill it at 1.0×
    # which, combined with cache_creation_tokens=0 on real transcripts, means
    # no double-billing. Operators who use the older GPT-4o-era 50% rate can
    # override via the `cache_multipliers` argument to `estimate_cost`.
    "openai":    {"read": 0.10, "create": 1.00},
    # Gemini context caching: reads at 10% of standard input. Storage cost is
    # billed per-hour and not modelled here.
    "google":    {"read": 0.10, "create": 1.00},
}

# Backwards-compat constants for callers that import these directly. They
# describe the *Anthropic* defaults — provider-aware code should use the
# `PROVIDER_CACHE_MULTIPLIERS` table above.
CACHE_READ_MULTIPLIER: float = PROVIDER_CACHE_MULTIPLIERS["anthropic"]["read"]
CACHE_WRITE_MULTIPLIER: float = PROVIDER_CACHE_MULTIPLIERS["anthropic"]["create"]

# Family aliases for each provider. Order matters: more specific tokens first
# so e.g. "claude-3-5-sonnet" matches "sonnet" not "claude" (we don't even
# need a generic "claude" alias — every Anthropic model includes haiku /
# sonnet / opus in its id).
_ANTHROPIC_FAMILIES = ("haiku", "sonnet", "opus")
_OPENAI_FAMILIES = ("gpt-5-nano", "gpt-5-mini", "gpt-5", "o4-mini", "o3")
_GEMINI_FAMILIES = (
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-pro",
)

# Back-compat: existing test imports `_FAMILY_ALIASES`.
_FAMILY_ALIASES = _ANTHROPIC_FAMILIES


def _coerce_pair(v: object) -> tuple[float, float] | None:
    """Best-effort coercion of a JSON value to (float, float). None on failure."""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return (float(v[0]), float(v[1]))
        except (TypeError, ValueError):
            return None
    return None


def load_env_rates() -> dict[str, tuple[float, float]]:
    """Read TOTAL_RECALL_RATES_JSON if set. Returns {} on missing/invalid."""
    raw = os.environ.get("TOTAL_RECALL_RATES_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        pair = _coerce_pair(v)
        if pair is not None:
            out[k.lower()] = pair
    return out


def detect_provider(model: str) -> str:
    """Classify a model name into one of: anthropic, openai, google, unknown.

    The classifier is purely a substring check on the lowercased model name —
    same approach as the family aliases. It is deliberately tolerant so that
    new model ids in the same family (``claude-sonnet-4-7``, ``gpt-5.4``,
    ``gemini-3.0-pro``) classify correctly without code changes.
    """
    name = (model or "").strip().lower()
    if not name:
        return "unknown"
    if "claude" in name or any(f in name for f in _ANTHROPIC_FAMILIES):
        return "anthropic"
    if "gpt-" in name or name.startswith("gpt") or name.startswith("o3") or name.startswith("o4"):
        return "openai"
    if "gemini" in name:
        return "google"
    return "unknown"


def _families_for_provider(provider: str) -> tuple[str, ...]:
    if provider == "anthropic":
        return _ANTHROPIC_FAMILIES
    if provider == "openai":
        return _OPENAI_FAMILIES
    if provider == "google":
        return _GEMINI_FAMILIES
    # Unknown providers still get the union, anthropic-first for back-compat.
    return _ANTHROPIC_FAMILIES + _OPENAI_FAMILIES + _GEMINI_FAMILIES


def resolve_rate(
    model: str,
    rates: Mapping[str, tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """Look up (input_per_Mtok, output_per_Mtok) for a model name.

    Resolution order:

    1. exact match in caller-supplied ``rates``
    2. exact match in :data:`DEFAULT_RATES`
    3. family alias for the *detected provider* (longest match first); checks
       caller-supplied ``rates`` then ``DEFAULT_RATES``
    4. ``TOTAL_RECALL_RATES_JSON`` env override (exact, then family)
    5. ``DEFAULT_RATES['default']``
    """
    name = (model or "").strip().lower()

    # 1. Caller-supplied exact match
    lower_rates: dict[str, tuple[float, float]] = (
        {k.lower(): v for k, v in rates.items()} if rates else {}
    )
    if name and name in lower_rates:
        return lower_rates[name]

    # 2. Exact match in DEFAULT_RATES
    if name in DEFAULT_RATES:
        return DEFAULT_RATES[name]

    # 3. Family alias for the detected provider (then any provider).
    provider = detect_provider(name)
    families = _families_for_provider(provider)
    for fam in families:
        if fam in name:
            if fam in lower_rates:
                return lower_rates[fam]
            if fam in DEFAULT_RATES:
                return DEFAULT_RATES[fam]

    # 4. Env override
    env_rates = load_env_rates()
    if name in env_rates:
        return env_rates[name]
    for fam in families:
        if fam in name and fam in env_rates:
            return env_rates[fam]

    # 5. Final fallback
    return DEFAULT_RATES["default"]


def estimate_cost(
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "default",
    rates: Mapping[str, tuple[float, float]] | None = None,
    cache_multipliers: Mapping[str, float] | None = None,
) -> float:
    """Estimate USD cost for a single (request, response) pair.

    Provider is detected from ``model`` and the matching entry in
    :data:`PROVIDER_CACHE_MULTIPLIERS` is applied unless the caller passes an
    explicit ``cache_multipliers`` mapping with ``read`` / ``create`` keys.

    Cached-input billing differs by provider — most notably OpenAI's
    ``cached_input`` field bills at 10% of standard input on the GPT-5
    family (older GPT-4o-era models used 50%; override via
    ``cache_multipliers`` if needed). Anthropic cache create is billed at
    125%; the other providers treat cache creation as implicit and free, so
    their ``create`` multiplier is 1.0 and the corresponding token field is
    normally zero.
    """
    in_rate, out_rate = resolve_rate(model, rates=rates)

    provider = detect_provider(model)
    mults = dict(PROVIDER_CACHE_MULTIPLIERS.get(provider, PROVIDER_CACHE_MULTIPLIERS["anthropic"]))
    if cache_multipliers:
        # Caller override wins on a per-key basis.
        for k, v in cache_multipliers.items():
            mults[k] = float(v)

    per = 1_000_000.0
    cost = 0.0
    cost += (input_tokens or 0) * in_rate / per
    cost += (cache_read_tokens or 0) * in_rate * mults.get("read", 0.10) / per
    cost += (cache_creation_tokens or 0) * in_rate * mults.get("create", 1.25) / per
    cost += (output_tokens or 0) * out_rate / per
    return cost


def parse_rate_arg(s: str) -> tuple[str, tuple[float, float]]:
    """Parse '--rate' CLI value 'name=in/out' → (name_lower, (in, out)).

    Raises ValueError on bad input.
    """
    if not isinstance(s, str) or "=" not in s:
        raise ValueError(f"expected 'name=in/out', got {s!r}")
    name, _, pair = s.partition("=")
    name = name.strip().lower()
    if not name:
        raise ValueError(f"empty model name in {s!r}")
    if "/" not in pair:
        raise ValueError(f"expected 'in/out' after '=' in {s!r}")
    in_s, _, out_s = pair.partition("/")
    try:
        in_v = float(in_s.strip())
        out_v = float(out_s.strip())
    except ValueError as e:
        raise ValueError(f"non-numeric rate in {s!r}: {e}") from e
    if in_v < 0 or out_v < 0:
        raise ValueError(f"negative rate in {s!r}")
    return name, (in_v, out_v)
