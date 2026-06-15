"""LLM refinement eval harness — measures machines NER and vocab quality.

Gating
------
Skips entirely unless ALL of:
  - env TOTAL_RECALL_LLM_EVAL=1
  - ollama reachable and model present (client.available == True)
  - tests.local.llm_eval_fixtures importable (in tests/local/, gitignored)

Model selection
---------------
  TOTAL_RECALL_LLM_EVAL_MODEL  — model to test (default: "gemma4:e2b")

Usage examples
--------------
  # default model
  TOTAL_RECALL_LLM_EVAL=1 uv run python -m pytest tests/integration/test_llm_eval.py -v -s

  # compare two models
  TOTAL_RECALL_LLM_EVAL=1 TOTAL_RECALL_LLM_EVAL_MODEL=gemma4:e2b  pytest ... -s > eval_e2b.txt
  TOTAL_RECALL_LLM_EVAL=1 TOTAL_RECALL_LLM_EVAL_MODEL=llama3.2:3b pytest ... -s > eval_3b.txt
"""
from __future__ import annotations

import os
import time

import pytest

# ---------------------------------------------------------------------------
# Gate: env var must be explicitly set
# ---------------------------------------------------------------------------

_EVAL_ENABLED = os.environ.get("TOTAL_RECALL_LLM_EVAL", "").strip() == "1"
_EVAL_MODEL = os.environ.get("TOTAL_RECALL_LLM_EVAL_MODEL", "gemma4:e2b").strip()

pytestmark = pytest.mark.skipif(
    not _EVAL_ENABLED,
    reason="set TOTAL_RECALL_LLM_EVAL=1 to run LLM eval (slow, requires ollama)",
)


# ---------------------------------------------------------------------------
# Fixture imports (gitignored — skip if absent)
# ---------------------------------------------------------------------------

def _load_fixtures():
    """Import fixture module from tests/local/; skip if absent."""
    try:
        from tests.local import llm_eval_fixtures as fx
        return fx
    except ImportError:
        pytest.skip("tests/local/llm_eval_fixtures.py not present — gitignored fixture")


# ---------------------------------------------------------------------------
# Client fixture — build once, skip if unavailable
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def eval_client():
    """Build an LLMClient for the eval model; skip if unavailable."""
    if not _EVAL_ENABLED:
        pytest.skip("TOTAL_RECALL_LLM_EVAL not set")

    try:
        from extractors.llm.client import LLMClient
    except ImportError as exc:
        pytest.skip(f"extractors.llm.client not importable: {exc}")

    client = LLMClient(provider="auto", model=_EVAL_MODEL)
    if not client.available:
        pytest.skip(
            f"LLM model {_EVAL_MODEL!r} not available via ollama "
            f"(daemon unreachable or model not pulled). "
            f"Run: ollama pull {_EVAL_MODEL}"
        )
    return client


# ---------------------------------------------------------------------------
# Helper: token overlap ratio
# ---------------------------------------------------------------------------

def _token_overlap(text: str, snippet: str) -> float:
    """Fraction of unique words in *text* that also appear in *snippet*."""
    if not text or not snippet:
        return 0.0

    def tokens(s: str) -> set[str]:
        import re
        return {w.lower() for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) > 1}

    text_tokens = tokens(text)
    snippet_tokens = tokens(snippet)
    if not text_tokens:
        return 0.0
    return len(text_tokens & snippet_tokens) / len(text_tokens)


# ---------------------------------------------------------------------------
# Test 1 — machines precision / recall
# ---------------------------------------------------------------------------

def test_machines_precision_recall(eval_client):
    """Assert precision >= 0.7; recall and F1 are printed as scorecard."""
    fx = _load_fixtures()

    try:
        from extractors.llm.refine_machines import refine_machines
    except ImportError as exc:
        pytest.skip(f"refine_machines not importable: {exc}")

    result = refine_machines(
        heuristic_machines=fx.MACHINES_LABELED,
        operator_email="dana@acme.example",
        client=eval_client,
        sample_contexts=fx.SAMPLE_CONTEXTS,
    )

    kept: set[str] = set(result.keys())
    ground_truth: set[str] = set(fx.GROUND_TRUTH_HOSTS)
    total_input: set[str] = set(fx.MACHINES_LABELED.keys())

    true_positive = kept & ground_truth
    false_positive = kept - ground_truth
    false_negative = ground_truth - kept

    precision = len(true_positive) / len(kept) if kept else 0.0
    recall = len(true_positive) / len(ground_truth) if ground_truth else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    print(f"\n=== MACHINES EVAL (model={_EVAL_MODEL}) ===")
    print(f"  Input:          {len(total_input)} tokens")
    print(f"  Kept by model:  {len(kept)}")
    print(f"  Ground truth:   {len(ground_truth)} real hosts")
    print(f"  True positives: {sorted(true_positive)}")
    print(f"  False positives:{sorted(false_positive)}")
    print(f"  False negatives:{sorted(false_negative)}")
    print(f"  Precision:      {precision:.3f}")
    print(f"  Recall:         {recall:.3f}")
    print(f"  F1:             {f1:.3f}")

    # Hard assertion: precision >= 0.70 (the model must not flood with noise).
    assert precision >= 0.70, (
        f"machines precision {precision:.3f} < 0.70 — "
        f"model kept too many noise tokens: {sorted(false_positive)}"
    )


# ---------------------------------------------------------------------------
# Test 2 — vocab echo_rate and define_coverage (scorecard only, no hard assert)
# ---------------------------------------------------------------------------

def test_vocab_quality_scorecard(eval_client):
    """Print echo_rate and define_coverage; no hard assertions (measurement only)."""
    fx = _load_fixtures()

    try:
        from extractors.llm.refine_ontology import refine_vocabulary_definitions
    except ImportError as exc:
        pytest.skip(f"refine_vocabulary_definitions not importable: {exc}")

    # Strip the eval-only _definable key before passing to the LLM.
    terms_clean = [
        {k: v for k, v in t.items() if not k.startswith("_")}
        for t in fx.VOCAB_TERMS
    ]

    results = refine_vocabulary_definitions(terms=terms_clean, client=eval_client)

    # Build lookup: term → returned definition
    def_map: dict[str, str | None] = {r["term"]: r.get("definition") for r in results}

    # echo_rate: fraction of non-null definitions whose token overlap with
    # their source snippet >= 0.6 (i.e., the model is parroting the snippet).
    non_null = {
        t["term"]: (t.get("context_snippet") or "", def_map.get(t["term"]))
        for t in fx.VOCAB_TERMS
        if def_map.get(t["term"]) is not None
    }
    echo_count = sum(
        1 for snippet, defn in non_null.values()
        if _token_overlap(defn, snippet) >= 0.6
    )
    echo_rate = echo_count / len(non_null) if non_null else 0.0

    # define_coverage: fraction of *definable* terms that got a non-null,
    # non-echo definition (i.e., the model produced real signal).
    definable = fx.DEFINABLE_TERMS
    good_defs = {
        term for term in definable
        if def_map.get(term) is not None
        and _token_overlap(def_map[term], next(
            t.get("context_snippet", "") for t in fx.VOCAB_TERMS if t["term"] == term
        )) < 0.6
    }
    define_coverage = len(good_defs) / len(definable) if definable else 0.0

    print(f"\n=== VOCAB EVAL (model={_EVAL_MODEL}) ===")
    for t in fx.VOCAB_TERMS:
        term = t["term"]
        defn = def_map.get(term)
        snippet = t.get("context_snippet", "")
        overlap = _token_overlap(defn or "", snippet)
        status = (
            "null" if defn is None
            else (f"echo({overlap:.2f})" if overlap >= 0.6 else f"ok({overlap:.2f})")
        )
        print(f"  {term!r:20s}  [{status}]  {(defn or '')[:80]}")
    print(f"  echo_rate:        {echo_rate:.3f}  (ideally low — model should paraphrase, not copy)")
    print(
        f"  define_coverage:  {define_coverage:.3f}  "
        "(ideally high — definable terms got real defs)"
    )
    print(f"  null_rate:        {1 - len(non_null)/len(fx.VOCAB_TERMS):.3f}")


# ---------------------------------------------------------------------------
# Test 3 — determinism (hard assert: two calls return identical keep-set)
# ---------------------------------------------------------------------------

def test_machines_determinism(eval_client):
    """Two back-to-back refine_machines calls must return identical keep-sets."""
    fx = _load_fixtures()

    try:
        from extractors.llm.refine_machines import refine_machines
    except ImportError as exc:
        pytest.skip(f"refine_machines not importable: {exc}")

    result_a = refine_machines(
        heuristic_machines=fx.MACHINES_LABELED,
        operator_email="dana@acme.example",
        client=eval_client,
        sample_contexts=fx.SAMPLE_CONTEXTS,
    )
    result_b = refine_machines(
        heuristic_machines=fx.MACHINES_LABELED,
        operator_email="dana@acme.example",
        client=eval_client,
        sample_contexts=fx.SAMPLE_CONTEXTS,
    )

    kept_a = frozenset(result_a.keys())
    kept_b = frozenset(result_b.keys())

    print(f"\n=== DETERMINISM (model={_EVAL_MODEL}) ===")
    print(f"  Run A keep-set: {sorted(kept_a)}")
    print(f"  Run B keep-set: {sorted(kept_b)}")
    if kept_a != kept_b:
        print(f"  DELTA A-B: {sorted(kept_a - kept_b)}")
        print(f"  DELTA B-A: {sorted(kept_b - kept_a)}")

    assert kept_a == kept_b, (
        f"refine_machines is non-deterministic at temperature=0.0 for model {_EVAL_MODEL!r}.\n"
        f"  Run A: {sorted(kept_a)}\n  Run B: {sorted(kept_b)}\n"
        f"  Delta A-B: {sorted(kept_a - kept_b)}\n"
        f"  Delta B-A: {sorted(kept_b - kept_a)}"
    )


# ---------------------------------------------------------------------------
# Test 4 — latency (printed as scorecard; no hard threshold)
# ---------------------------------------------------------------------------

def test_machines_latency(eval_client):
    """Time one refine_machines call; print wall-clock latency as scorecard."""
    fx = _load_fixtures()

    try:
        from extractors.llm.refine_machines import refine_machines
    except ImportError as exc:
        pytest.skip(f"refine_machines not importable: {exc}")

    t0 = time.monotonic()
    result = refine_machines(
        heuristic_machines=fx.MACHINES_LABELED,
        operator_email="dana@acme.example",
        client=eval_client,
        sample_contexts=fx.SAMPLE_CONTEXTS,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    print(f"\n=== LATENCY (model={_EVAL_MODEL}) ===")
    print(
        f"  refine_machines wall-clock: {elapsed_ms:.0f} ms  "
        f"({len(fx.MACHINES_LABELED)} input tokens)"
    )
    print(f"  kept: {len(result)}/{len(fx.MACHINES_LABELED)}")


# ---------------------------------------------------------------------------
# Scorecard summary — runs last (soft, always passes if gate is open)
# ---------------------------------------------------------------------------

def test_print_scorecard(eval_client):
    """Print consolidated scorecard; no assertions — pure measurement."""
    fx = _load_fixtures()

    _machines_ok = True
    _vocab_ok = True

    try:
        from extractors.llm.refine_machines import refine_machines
        from extractors.llm.refine_ontology import refine_vocabulary_definitions
    except ImportError as exc:
        pytest.skip(f"LLM modules not importable: {exc}")

    # Machines
    t0 = time.monotonic()
    machines_result = refine_machines(
        heuristic_machines=fx.MACHINES_LABELED,
        operator_email="dana@acme.example",
        client=eval_client,
        sample_contexts=fx.SAMPLE_CONTEXTS,
    )
    machines_ms = (time.monotonic() - t0) * 1000.0

    kept = set(machines_result.keys())
    gt = set(fx.GROUND_TRUTH_HOSTS)
    tp = kept & gt
    fp = kept - gt
    fn = gt - kept
    prec = len(tp) / len(kept) if kept else 0.0
    rec = len(tp) / len(gt) if gt else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

    # Vocab
    terms_clean = [
        {k: v for k, v in t.items() if not k.startswith("_")}
        for t in fx.VOCAB_TERMS
    ]
    t0 = time.monotonic()
    vocab_result = refine_vocabulary_definitions(terms=terms_clean, client=eval_client)
    vocab_ms = (time.monotonic() - t0) * 1000.0

    def_map = {r["term"]: r.get("definition") for r in vocab_result}
    non_null = {
        t["term"]: (t.get("context_snippet") or "", def_map.get(t["term"]))
        for t in fx.VOCAB_TERMS
        if def_map.get(t["term"]) is not None
    }
    echo_count = sum(
        1 for snippet, defn in non_null.values()
        if _token_overlap(defn, snippet) >= 0.6
    )
    echo_rate = echo_count / len(non_null) if non_null else 0.0
    definable = fx.DEFINABLE_TERMS
    good_defs = {
        term for term in definable
        if def_map.get(term) is not None
        and _token_overlap(def_map[term], next(
            t.get("context_snippet", "") for t in fx.VOCAB_TERMS if t["term"] == term
        )) < 0.6
    }
    define_coverage = len(good_defs) / len(definable) if definable else 0.0

    print(f"\n{'='*60}")
    print(f"=== LLM EVAL SCORECARD (model={_EVAL_MODEL}) ===")
    print(f"{'='*60}")
    print("  MACHINES NER")
    print(f"    precision:        {prec:.3f}   (>= 0.70 required)")
    print(f"    recall:           {rec:.3f}")
    print(f"    F1:               {f1:.3f}")
    print(f"    false_positives:  {sorted(fp)}")
    print(f"    false_negatives:  {sorted(fn)}")
    print(f"    latency:          {machines_ms:.0f} ms")
    print("  VOCAB DEFINITIONS")
    print(f"    echo_rate:        {echo_rate:.3f}   (lower = less parroting)")
    print(f"    define_coverage:  {define_coverage:.3f}   (higher = more real defs)")
    print(f"    null_rate:        {1 - len(non_null)/len(fx.VOCAB_TERMS):.3f}")
    print(f"    latency:          {vocab_ms:.0f} ms")
    print(f"{'='*60}")
