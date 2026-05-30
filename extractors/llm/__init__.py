"""Local-LLM refinement layer for operator-profile extractors.

This package provides a thin, dependency-free client for local LLM inference
(ollama / llama.cpp / vLLM) plus a SQLite-backed content-hash cache so
repeated calls on unchanged input are free.

Key design constraints:
- Off by default (provider='auto' silently skips when ollama is absent).
- Local-only — transcripts never leave the machine.
- Graceful absence — any failure returns None; heuristics remain authoritative.
- Cold-path only — never called from Stop-hook hot path.

Public API::

    from extractors.llm import LLMClient, LLMCache, get_default_client

    client = get_default_client()
    if client.available:
        result = client.generate_json(system="...", user="...")
"""

from extractors.llm.cache import LLMCache
from extractors.llm.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    LLMClient,
    autoselect_model,
    get_default_client,
)

__all__ = [
    "LLMClient",
    "LLMCache",
    "get_default_client",
    "autoselect_model",
    "DEFAULT_MODEL",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
]
