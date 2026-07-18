"""Unit tests for product-owned ollama runtime (no network, no real ollama)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def test_embed_model_tag_strips_legacy_hf(monkeypatch: pytest.MonkeyPatch) -> None:
    from vec import runtime

    monkeypatch.setenv("TOTAL_RECALL_EMBED_MODEL", "Alibaba-NLP/gte-modernbert-base")
    assert runtime.embed_model_tag() == "qwen3-embedding:0.6b"


def test_want_embed_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from vec import runtime

    monkeypatch.delenv("TOTAL_RECALL_VEC", raising=False)
    assert runtime.want_embed() is True
    monkeypatch.setenv("TOTAL_RECALL_VEC", "0")
    assert runtime.want_embed() is False


def test_want_chat_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from vec import runtime

    monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "none")
    assert runtime.want_chat() is False
    monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "auto")
    assert runtime.want_chat() is True


def test_ensure_skips_when_both_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from vec import runtime

    monkeypatch.setenv("TOTAL_RECALL_VEC", "0")
    monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "none")
    st = runtime.ensure_product_ollama()
    assert st["daemon"] is False
    assert st["embed"] is False
    assert st["chat"] is False


def test_ensure_raises_when_daemon_down_and_no_bin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vec import runtime

    monkeypatch.setenv("TOTAL_RECALL_VEC", "1")
    monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "none")
    monkeypatch.setattr(runtime, "daemon_reachable", lambda *a, **k: False)
    monkeypatch.setattr(runtime, "resolve_ollama_bin", lambda: None)
    monkeypatch.setattr(runtime, "_try_bash_provision", lambda: False)
    with pytest.raises(RuntimeError, match="Product ollama"):
        runtime.ensure_product_ollama(embed=True, chat=False)


def test_model_present_fuzzy() -> None:
    from vec import runtime

    names = ["qwen3-embedding:0.6b", "qwen3.5:2b"]
    assert runtime.model_present("qwen3-embedding:0.6b", names)
    assert runtime.model_present("qwen3-embedding", names)
