"""Live CPU smoke test for the default ollama + qwen3.5:2b refinement path.

Unlike the mocked ``test_llm_client.py``, this drives the REAL client against a
running ollama daemon with the default model pulled — the exact zero-config path
an operator gets (auto provider, qwen3.5:2b, CPU). It auto-detects the
environment:

* If ``TOTAL_RECALL_LLM_PROVIDER=none`` or the daemon / model are unavailable,
  every test SKIPS, so the default ``pytest`` run stays green on any machine
  (CI included).
* When the daemon is up and qwen3.5:2b is pulled, the tests RUN and assert the
  default resolves to qwen3.5:2b and real structured generation + machine
  refinement work end-to-end on CPU.

This is the "fully tested no matter what" guard for v1.2.0: it ships in the
normal suite and exercises the live CPU path whenever the environment supports
it. Quality scoring (precision/recall/define-coverage) is graded separately by
the gated harness ``tests/integration/test_llm_eval.py``; this file only proves
the path executes and honours its structural contracts.
"""
from __future__ import annotations

import os

import pytest

from extractors.llm.client import DEFAULT_MODEL, LLMClient, get_default_client


def _live_available() -> bool:
    if os.environ.get("TOTAL_RECALL_LLM_PROVIDER") == "none":
        return False
    try:
        return bool(LLMClient(provider="auto", model=DEFAULT_MODEL).available)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _live_available(),
    reason="live ollama daemon + qwen3.5:2b not available — skipping live CPU smoke",
)


def _machines_dict(*hosts: str) -> dict[str, dict]:
    """Build the {hostname: record} shape refine_machines expects."""
    return {
        h: {"hostname": h, "role": "", "lan_ip": "", "public_ip": ""} for h in hosts
    }


def test_default_model_is_qwen() -> None:
    """The shipped default is qwen3.5:2b (locked contract)."""
    assert DEFAULT_MODEL == "qwen3.5:2b"


def test_default_client_resolves_qwen_and_is_available() -> None:
    c = get_default_client()
    assert c.model == "qwen3.5:2b"
    assert c.available is True


def test_live_generate_json_returns_structured_dict() -> None:
    """A real CPU generation through the default client yields valid JSON."""
    c = get_default_client()
    out = c.generate_json(
        system="You classify tokens. Return JSON only.",
        user='Is "relay-eu-west" a server hostname? '
        'Return {"hostname": true} or {"hostname": false}.',
        schema={
            "type": "object",
            "properties": {"hostname": {"type": "boolean"}},
            "required": ["hostname"],
        },
    )
    assert isinstance(out, dict), f"expected dict, got {type(out).__name__}"
    assert "hostname" in out
    assert isinstance(out["hostname"], bool)


def test_live_refine_machines_runs_and_returns_subset() -> None:
    """refine_machines runs live on CPU and never invents hostnames.

    refine_machines takes {hostname: record} and returns a same-shaped dict
    whose keys are a subset of the input — the refiner filters, never
    hallucinates. Keep/drop judgment quality is graded by the gated eval
    harness, not here.
    """
    from extractors.llm.refine_machines import refine_machines

    candidates = _machines_dict("relay-eu-west", "db-prod-01", "nas-local")
    kept = refine_machines(candidates, client=get_default_client())
    assert isinstance(kept, dict)
    assert set(kept) <= set(candidates), (
        f"refiner returned hosts not in the input: {set(kept) - set(candidates)}"
    )


def test_live_refine_machines_fail_open_when_disabled() -> None:
    """With a disabled client the refiner fails open: input returned unchanged.

    Builds its own provider=none client (independent of the live daemon) to
    verify the fail-open contract that protects every refinement call site.
    """
    from extractors.llm.refine_machines import refine_machines

    disabled = LLMClient(provider="none")
    cands = _machines_dict("host-a", "host-b")
    out = refine_machines(cands, client=disabled)
    assert out == cands
