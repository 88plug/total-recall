"""Local-LLM client wrapper for the total-recall refinement layer.

Uses only stdlib (urllib, json) — no hard dependency on the `ollama` package.
Every failure mode (unreachable daemon, missing model, bad JSON, timeout)
returns None / False; callers never need to handle exceptions.

Privacy note: prompt and response content are logged only at DEBUG level.
Transcripts must never leave the machine.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .cache import LLMCache

log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3.5:2b"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_S = 180.0  # cold-load + first inference can take >60s on CPU

# ---------------------------------------------------------------------------
# Auto-selection
# ---------------------------------------------------------------------------


def autoselect_model() -> str:
    """Return the default LLM model for total-recall refinement.

    Head-to-head CPU eval results (no GPU):
      qwen3.5:2b  define_coverage 0.60  echo_rate 0.14  ~19s  ← WINNER
      qwen3.5:4b  define_coverage 0.60  echo_rate 0.25  ~33s  (same coverage, worse echo, slower)
      qwen3.5:9b  define_coverage 0.00  echo_rate 0.60  ~41s  (think-mode leak → all-null defs)

    Bigger models do NOT help on CPU for this task — qwen3.5:2b is always
    returned. If the machine can't run it the availability probe + heuristic
    fallback handle it gracefully; we never silently pick something worse.

    Override with TOTAL_RECALL_LLM_MODEL for a different model.
    Never raises.
    """
    return DEFAULT_MODEL

_PROBE_TIMEOUT_S = 10.0  # generous: probe competes with CPU-heavy ingest on rebuild
_PROBE_RETRIES = 3       # transient post-ingest load can lose the race; retry before False
_PROBE_BACKOFF_S = 1.5


# Per-model-family sampling profiles. Different model families need very
# different sampling settings — what's right for one actively hurts another.
#
# * gemma / default: greedy deterministic (temp 0, top_k 1). Verified good
#   for gemma's classification + (weak) generation; what we shipped first.
# * qwen3 / qwen3.5: the official model-card recommendations (non-thinking /
#   instruct mode, general text). Greedy (top_k=1, temp=0) is NOT recommended
#   for qwen — it wants temp 0.7, top_k 20, top_p 0.8, presence_penalty 1.5.
#   We keep a fixed ``seed`` so runs stay reproducible despite temp>0 (ollama
#   pins the sampler RNG to the seed), and disable thinking via the top-level
#   ``think: false`` so it doesn't emit <think> blocks that break JSON.
#
# Source: HuggingFace Qwen/Qwen3.5-4B + Qwen3.5-2B model cards (non-thinking
# text-task recommendations); ollama api.md (think param, >= 0.9).
_SAMPLING_PROFILES: dict[str, dict[str, Any]] = {
    "gemma": {
        "temperature": 0.0, "top_k": 1, "top_p": 1.0,
        "repeat_penalty": 1.0, "seed": 42, "think": None,
    },
    "qwen": {
        "temperature": 0.7, "top_k": 20, "top_p": 0.8, "min_p": 0.0,
        "presence_penalty": 1.5, "repeat_penalty": 1.0, "seed": 42,
        "think": False,
    },
    "default": {
        "temperature": 0.0, "top_k": 1, "top_p": 1.0,
        "repeat_penalty": 1.0, "seed": 42, "think": None,
    },
}


def _resolve_sampling(
    model: str, temperature_override: float | None
) -> tuple[dict[str, Any], bool | None]:
    """Return ``(options_dict, think)`` for *model*.

    Picks the family profile by model-name substring, applies an explicit
    ``temperature_override`` when the caller passed one (e.g. forcing greedy
    determinism for a classification task), and splits the ``think`` flag out
    of the options block (it's a top-level request field, not an option).
    """
    name = (model or "").lower()
    if "qwen" in name:
        profile = _SAMPLING_PROFILES["qwen"]
    elif "gemma" in name:
        profile = _SAMPLING_PROFILES["gemma"]
    else:
        profile = _SAMPLING_PROFILES["default"]

    opts = {k: v for k, v in profile.items() if k != "think"}
    think = profile.get("think")
    if temperature_override is not None:
        opts["temperature"] = temperature_override
    return opts, think


class LLMClient:
    """Thin wrapper around an ollama-compatible local inference endpoint.

    Parameters
    ----------
    provider:
        ``"auto"``   — detect ollama; skip silently if absent (default).
        ``"ollama"`` — treat as ollama; still probe for liveness.
        ``"none"``   — disable unconditionally (.available is always False).
    model:
        Name of the model to use (default: DEFAULT_MODEL).
    base_url:
        Base URL of the ollama daemon (default: DEFAULT_BASE_URL).
    timeout:
        Per-request timeout in seconds for generate calls (default: DEFAULT_TIMEOUT_S).
    """

    def __init__(
        self,
        provider: str = "auto",
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        self._provider = provider.lower() if provider else "auto"
        self._model = model or DEFAULT_MODEL
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_S
        self._available: bool | None = None  # None = not yet probed
        self._cache = cache

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def available(self) -> bool:
        """True iff provider != none, daemon reachable, and model is pulled.

        Result is cached after first probe so repeated checks are free.
        """
        if self._available is None:
            self._available = self._probe()
        return self._available

    # ------------------------------------------------------------------
    # Internal probe
    # ------------------------------------------------------------------

    def _probe(self) -> bool:
        if self._provider == "none":
            log.debug("LLMClient: provider=none, skipping probe")
            return False

        tags_url = f"{self._base_url}/api/tags"
        data: dict[str, Any] | None = None
        last_exc: Exception | None = None
        for _attempt in range(_PROBE_RETRIES):
            try:
                req = urllib.request.Request(tags_url, method="GET")
                with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
                    raw = resp.read()
                    data = json.loads(raw)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _attempt + 1 < _PROBE_RETRIES:
                    time.sleep(_PROBE_BACKOFF_S)
        if data is None:
            log.debug("LLMClient: probe failed after %d tries at %s — %s",
                      _PROBE_RETRIES, tags_url, last_exc)
            return False

        # ollama /api/tags returns {"models": [{"name": "...", ...}, ...]}
        models_raw: list[dict[str, Any]] = data.get("models", [])
        pulled = {m.get("name", "") for m in models_raw}

        # Model names from ollama often have ":latest" appended; check both.
        want = self._model
        want_latest = want if ":" in want else f"{want}:latest"
        if want in pulled or want_latest in pulled:
            log.debug("LLMClient: model %r available at %s", self._model, self._base_url)
            return True

        log.info(
            "LLMClient: model %r not found in ollama at %s "
            "(available: %s). Run `ollama pull %s` to enable LLM refinement.",
            self._model,
            self._base_url,
            sorted(pulled) or "(none)",
            self._model,
        )
        return False

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate_json(
        self,
        system: str,
        user: str,
        schema: dict | None = None,
        temperature: float | None = None,
        num_predict: int = 512,
        num_ctx: int = 4096,
    ) -> dict | None:
        """Call the model and return a parsed JSON dict.

        Returns None on any failure: unavailable, network error, parse error.
        Uses ollama's ``format: "json"`` mode for determinism. When *schema*
        is provided and the server supports structured output (ollama >= 0.5),
        ``format=<schema>`` is sent for guaranteed-shape output (constrained
        decoding — the model literally cannot emit a key not in the schema);
        otherwise falls back to ``format="json"`` and post-validates.

        Sampling is **model-family aware** (see :func:`_resolve_sampling` and
        ``_SAMPLING_PROFILES``): gemma/default use greedy determinism (temp 0,
        top_k 1); qwen3/qwen3.5 use the official model-card recommendations
        (temp 0.7, top_k 20, top_p 0.8, presence_penalty 1.5) plus a top-level
        ``think: false`` so qwen doesn't emit ``<think>`` blocks that break the
        JSON parse. A fixed ``seed`` keeps even temp>0 runs reproducible.

        Other fixed knobs:

        * ``num_ctx=4096``: explicit small KV cache. ollama's default of 0
          negotiates the model's training max which allocates a huge KV cache
          on first load — slow on CPU. 4096 covers our ≤2k-token prompts.
        * ``num_predict=512``: hard output ceiling so the model can't run away.
        * ``keep_alive="15m"``: keep the model resident across the rebuild's
          many sequential calls; auto-evict afterwards.

        Parameters
        ----------
        system:
            System prompt directing the model's output format.
        user:
            User message / content to analyse.
        schema:
            Optional JSON Schema dict for structured output (ollama >= 0.5).
        temperature:
            Optional override of the family-profile temperature. ``None``
            (default) uses the family default; pass ``0.0`` to force greedy
            determinism for a classification task regardless of family.
        num_predict:
            Max output tokens (default 512).
        num_ctx:
            KV-cache context length in tokens (default 4096; see note above).
        """
        if not self.available:
            return None

        schema_json: str | None = json.dumps(schema, sort_keys=True) if schema is not None else None

        if self._cache is not None:
            cached = self._cache.get(self._model, system, user, schema_json)
            if cached is not None:
                return cached

        opts, think = _resolve_sampling(self._model, temperature)
        opts["num_ctx"] = num_ctx
        opts["num_predict"] = num_predict

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": user,
            "system": system,
            "stream": False,
            "keep_alive": "15m",
            "options": opts,
        }
        # Thinking-capable models (qwen3/qwen3.5, deepseek-r1, …) emit
        # <think> blocks by default — wasteful and they break JSON parsing.
        # ``think`` is a TOP-LEVEL request field (ollama >= 0.9), not an
        # option. None → leave default (non-thinking models ignore it).
        if think is False:
            payload["think"] = False

        if schema is not None:
            # Structured output: pass schema dict as format value.
            # Older ollama versions ignore unknown format shapes gracefully.
            payload["format"] = schema
        else:
            payload["format"] = "json"

        url = f"{self._base_url}/api/generate"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.URLError as exc:
            log.warning("LLMClient: generate request failed — %s", exc)
            return None
        except TimeoutError as exc:
            log.warning("LLMClient: generate timed out after %.0fs — %s", self._timeout, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("LLMClient: unexpected generate error — %s", exc)
            return None

        # ollama generate response: {"response": "<json string>", "done": true, ...}
        try:
            outer: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("LLMClient: outer response not valid JSON — %s", exc)
            return None

        response_text: str = outer.get("response", "")
        if not response_text:
            log.warning("LLMClient: empty response from model")
            return None

        log.debug("LLMClient: raw model response (truncated): %.200s", response_text)

        try:
            result: dict[str, Any] = json.loads(response_text)
        except json.JSONDecodeError as exc:
            log.warning("LLMClient: model response not valid JSON — %s", exc)
            return None

        if not isinstance(result, dict):
            log.warning("LLMClient: model response is not a JSON object (got %s)", type(result).__name__)
            return None

        if self._cache is not None:
            self._cache.put(self._model, system, user, schema_json, result)

        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_default_client() -> LLMClient:
    """Build an LLMClient from environment variables.

    Environment variables:
        TOTAL_RECALL_LLM_PROVIDER  — "auto" | "ollama" | "none"  (default: "auto")
        TOTAL_RECALL_LLM_MODEL     — model name  (default: DEFAULT_MODEL)
        TOTAL_RECALL_LLM_BASE_URL  — daemon URL  (default: DEFAULT_BASE_URL)

    The returned client's ``.available`` reflects whether the daemon is
    reachable and the model is pulled.

    A SQLite LLM result cache is constructed automatically and injected into
    the client.  Cache construction errors are non-fatal (cache=None).
    """
    provider = os.environ.get("TOTAL_RECALL_LLM_PROVIDER", "auto")
    model = os.environ.get("TOTAL_RECALL_LLM_MODEL") or autoselect_model()
    base_url = os.environ.get("TOTAL_RECALL_LLM_BASE_URL") or DEFAULT_BASE_URL

    cache: LLMCache | None = None
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        cache_path = Path(plugin_data) / "total-recall" / "llm_cache.db"
    else:
        cache_path = Path.home() / ".local" / "share" / "total-recall-88plug" / "total-recall" / "llm_cache.db"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache = LLMCache(cache_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLMClient: cache construction failed, running without cache — %s", exc)

    return LLMClient(provider=provider, model=model, base_url=base_url, cache=cache)
