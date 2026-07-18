"""Embedding wrapper — **product-owned ollama only**.

Product contract (format v2, 2026-07):
  * Dense vectors via total-recall's managed ollama (bundled binary under the
    plugin data dir → ``ollama serve`` → HTTP). Not in-process ONNX/fastembed.
  * Default model: ``qwen3-embedding:0.6b`` (not chat ``qwen3.5:2b``).
  * Auto-ensure: first embed/rebuild starts the product daemon + pulls the
    embed model when missing (see :mod:`vec.runtime`).
  * Override: ``TOTAL_RECALL_EMBED_MODEL`` (ollama tag) or ``Embedder(model=tag)``.
  * Base URL: ``TOTAL_RECALL_LLM_BASE_URL`` (default ``http://localhost:11434``).

Qwen3-embedding query convention (``as_query=True``):
  Instruct: Given a web search query, retrieve relevant passages that answer the query
  Query:{query}
Documents: raw text (no prefix).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

RECOMMENDED_OLLAMA_EMBED = "qwen3-embedding:0.6b"

QWEN3_QUERY_INSTRUCT = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
    "Query:"
)

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
_OLLAMA_PROBE_TIMEOUT_S = 10.0
_OLLAMA_EMBED_TIMEOUT_S = 120.0

# GPU-hard defaults for embed requests (override via env).
# num_gpu: layers to offload — 999 ≈ all layers (ollama treats large values as "all").
# keep_alive -1: pin model in VRAM until explicit stop (no 5m pussy unload mid-backfill).
# num_ctx: embed chunks are small; 8k is plenty and frees VRAM vs model-max 32k.
def _embed_keep_alive() -> str | int:
    raw = (os.environ.get("TOTAL_RECALL_EMBED_KEEP_ALIVE") or "-1").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _embed_options() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "num_gpu": int(os.environ.get("TOTAL_RECALL_OLLAMA_NUM_GPU") or "999"),
        "num_ctx": int(os.environ.get("TOTAL_RECALL_EMBED_NUM_CTX") or "8192"),
        "num_batch": int(os.environ.get("TOTAL_RECALL_EMBED_NUM_BATCH") or "512"),
    }
    thr = (os.environ.get("TOTAL_RECALL_EMBED_NUM_THREAD") or "").strip()
    if thr.isdigit() and int(thr) > 0:
        opts["num_thread"] = int(thr)
    return opts

# Known dims (live-verified where noted). Others discover on first embed.
_KNOWN_DIMS: dict[str, int] = {
    "qwen3-embedding:0.6b": 1024,
    "qwen3-embedding:4b": 2560,
    "qwen3-embedding:8b": 4096,
    "qwen3-embedding:latest": 4096,
    "qwen3-embedding": 4096,
    "granite-embedding:30m": 384,
    "granite-embedding:278m": 768,
    "embeddinggemma:300m": 768,
    "embeddinggemma": 768,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "nomic-embed-text-v2-moe:latest": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}

_QUERY_PREFIX: dict[str, str] = {
    "nomic-embed-text": "search_query: ",
    "nomic-embed-text:latest": "search_query: ",
    "qwen3-embedding:0.6b": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding:4b": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding:8b": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding:latest": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding": QWEN3_QUERY_INSTRUCT,
}

_DOC_PREFIX: dict[str, str] = {
    "nomic-embed-text": "search_document: ",
    "nomic-embed-text:latest": "search_document: ",
}


def _query_prefix_for(model: str) -> str:
    if model in _QUERY_PREFIX:
        return _QUERY_PREFIX[model]
    if model.startswith("qwen3-embedding"):
        return QWEN3_QUERY_INSTRUCT
    return ""


def _doc_prefix_for(model: str) -> str:
    return _DOC_PREFIX.get(model, "")


# ----------------------------------------------------------------------------
# ollama HTTP
# ----------------------------------------------------------------------------


def _ollama_base_url() -> str:
    return (os.environ.get("TOTAL_RECALL_LLM_BASE_URL") or _OLLAMA_DEFAULT_BASE_URL).rstrip("/")


def _ollama_list_models(base_url: str) -> list[dict[str, Any]] | None:
    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=_OLLAMA_PROBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None
    models = data.get("models")
    return models if isinstance(models, list) else None


def _is_embedding_model(m: dict[str, Any]) -> bool:
    caps = m.get("capabilities") or []
    return isinstance(caps, list) and "embedding" in caps


def _embedding_dim_hint(m: dict[str, Any]) -> int | None:
    details = m.get("details") or {}
    if isinstance(details, dict):
        n = details.get("embedding_length")
        if isinstance(n, int) and n > 0:
            return n
    return None


def _embedding_names(base_url: str) -> list[str] | None:
    """Pulled embedding-capable model names, or None if daemon unreachable."""
    models = _ollama_list_models(base_url)
    if models is None:
        return None
    return [
        str(m.get("name") or "")
        for m in models
        if isinstance(m, dict) and _is_embedding_model(m) and m.get("name")
    ]


def _looks_like_legacy_hf_embed(name: str) -> bool:
    """True for pre-v2 TOTAL_RECALL_EMBED_MODEL values (HuggingFace / ONNX ids).

    Ollama tags (``qwen3-embedding:0.6b``, ``granite-embedding:30m``) are fine.
    """
    n = name.strip()
    if not n:
        return False
    # HF org/model — never a valid ollama tag for this path.
    if "/" in n and not n.startswith("http"):
        return True
    # Bare retired fastembed defaults (no ollama colon-tag form).
    low = n.lower().split(":")[0]
    return low in {
        "gte-modernbert-base",
        "gte-modernbert",
        "bge-small-en-v1.5",
        "bge-small-en",
        "nomic-embed-text-v1.5",
        "alibaba-nlp",
    }


def _legacy_embed_model_error(want: str) -> RuntimeError:
    return RuntimeError(
        f"TOTAL_RECALL_EMBED_MODEL={want!r} is a legacy fastembed/ONNX id. "
        f"Dense embeds are ollama-only (format v2). Unset the env var (or remove it "
        f"from plugin MCP config) and pull the recommended model:\n"
        f"    ollama pull {RECOMMENDED_OLLAMA_EMBED}\n"
        f"Then: total-recall rebuild --yes"
    )


def _pick_ollama_embed_model(base_url: str, want: str | None) -> str | None:
    """Return an embedding-capable pulled model name, or None if none / unreachable.

    If ``want`` is set and not pulled as an embedding model, raises RuntimeError
    (no silent substitute).
    """
    if want and _looks_like_legacy_hf_embed(want):
        raise _legacy_embed_model_error(want)

    names = _embedding_names(base_url)
    if names is None:
        return None
    if not names:
        if want:
            raise RuntimeError(
                f"No embedding-capable models at {base_url}; cannot use {want!r}. "
                f"Pull: ollama pull {want or RECOMMENDED_OLLAMA_EMBED}"
            )
        return None

    name_set = set(names)
    if want:
        want_latest = want if ":" in want else f"{want}:latest"
        if want in name_set:
            return want
        if want_latest in name_set:
            return want_latest
        raise RuntimeError(
            f"Embedding model {want!r} is not pulled (or not embedding-capable) at "
            f"{base_url}. Available: {names}. Pull: ollama pull {want}"
        )

    if RECOMMENDED_OLLAMA_EMBED in name_set:
        return RECOMMENDED_OLLAMA_EMBED
    for name in sorted(names):
        if name.startswith("qwen3-embedding:0.6b"):
            return name

    # Fallback: smallest by size among embedding-capable
    models = _ollama_list_models(base_url) or []
    emb = [m for m in models if isinstance(m, dict) and _is_embedding_model(m)]
    emb.sort(key=lambda m: m.get("size", 0) or 0)
    return (emb[0].get("name") if emb else None) or None


def _ollama_embed(base_url: str, model: str, texts: list[str]) -> list[list[float]]:
    opts = {k: v for k, v in _embed_options().items() if v is not None}
    payload: dict[str, Any] = {
        "model": model,
        "input": texts,
        "truncate": False,
        # Pin resident for bulk backfill / hybrid search (override with env).
        "keep_alive": _embed_keep_alive(),
        "options": opts,
    }
    req = urllib.request.Request(
        f"{base_url}/api/embed",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_OLLAMA_EMBED_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ollama embed request to {base_url} failed: {exc}") from exc
    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list):
        raise RuntimeError(f"ollama embed response missing 'embeddings': {data!r}")
    return [[float(x) for x in row] for row in embeddings]


class Embedder:
    """Ollama-only embedder. Construct cheap; load on first ``.embed()`` / ``.dim()``.

    Args:
        model: Optional ollama tag override (e.g. ``qwen3-embedding:0.6b``).
            If None, pick via env / recommended / smallest embedding-capable.
    """

    def __init__(self, model: str | None = None) -> None:
        self._forced_model = model
        self.model = model or ""
        self._backend: str | None = None
        self._ollama_base_url: str | None = None
        self._dim: int | None = _KNOWN_DIMS.get(model) if model else None
        self._query_prefix: str = _query_prefix_for(model) if model else ""
        self._doc_prefix: str = _doc_prefix_for(model) if model else ""

    def _load(self) -> None:
        if self._backend is not None:
            return

        base_url = _ollama_base_url()
        want = self._forced_model or os.environ.get("TOTAL_RECALL_EMBED_MODEL")
        # Product-owned runtime: start our daemon + pull embed model if needed.
        if want is None or not _looks_like_legacy_hf_embed(want):
            try:
                from vec.runtime import ensure_product_ollama

                ensure_product_ollama(embed=True, chat=False, pull=True)
            except Exception as exc:  # noqa: BLE001
                # Soft: still try HTTP in case daemon is up but ensure failed mid-pull.
                if not _ollama_list_models(base_url):
                    raise RuntimeError(
                        f"Product ollama not ready for embeds: {exc}"
                    ) from exc

        chosen = _pick_ollama_embed_model(base_url, want)
        if chosen is None:
            raise RuntimeError(
                f"No embedding-capable ollama model at {base_url}. "
                f"total-recall auto-provisions product ollama; if this persists:\n"
                f"    bash scripts/llm-setup.sh\n"
                f"    # or: ollama pull {RECOMMENDED_OLLAMA_EMBED}"
            )
        self._load_ollama(chosen, base_url)

    def _load_ollama(self, model: str, base_url: str) -> None:
        self._backend = "ollama"
        self.model = model
        self._ollama_base_url = base_url
        self._query_prefix = _query_prefix_for(model)
        self._doc_prefix = _doc_prefix_for(model)
        if self._dim is None:
            self._dim = _KNOWN_DIMS.get(model)
            models = _ollama_list_models(base_url) or []
            for m in models:
                if isinstance(m, dict) and m.get("name") == model:
                    hint = _embedding_dim_hint(m)
                    if hint:
                        self._dim = hint
                    break

    def embed(self, texts: list[str], as_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        self._load()

        if as_query and self._query_prefix:
            texts = [self._query_prefix + t for t in texts]
        elif not as_query and self._doc_prefix:
            texts = [self._doc_prefix + t for t in texts]

        assert self._ollama_base_url is not None
        out = _ollama_embed(self._ollama_base_url, self.model, texts)
        if self._dim is None and out:
            self._dim = len(out[0])
        return out

    def dim(self) -> int:
        if self._dim is not None:
            return self._dim
        self.embed(["."])
        if self._dim is None:
            raise RuntimeError("could not discover embedding dim")
        return self._dim

    @property
    def backend(self) -> str | None:
        if self._backend is None:
            try:
                self._load()
            except Exception:  # noqa: BLE001
                return None
        return self._backend

    def identity(self) -> str:
        self._load()
        return f"{self._backend}:{self.model}"


# ----------------------------------------------------------------------------
# Chunking (backend-agnostic)
# ----------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4

_PARA_RE = re.compile(r"\n\s*\n")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def _split_sentences(text: str) -> list[str]:
    parts = _PARA_RE.split(text)
    out: list[str] = []
    for para in parts:
        para = para.strip()
        if not para:
            continue
        for s in _SENT_RE.split(para):
            s = s.strip()
            if s:
                out.append(s)
    return out


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def chunk_for_embedding(text: str, max_tokens: int = 400, overlap: int = 50) -> list[str]:
    """Split ``text`` into embedding-sized chunks (sentence-aware)."""
    if not text or not text.strip():
        return []

    max_chars = max_tokens * _CHARS_PER_TOKEN
    overlap_chars = max(0, overlap) * _CHARS_PER_TOKEN
    sentences = _split_sentences(text)
    if not sentences:
        return []

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    chunk_protected: list[bool] = []

    def append_chunk(piece: str, protected: bool) -> None:
        chunks.append(piece)
        chunk_protected.append(protected)

    for sent in sentences:
        if len(sent) > max_chars:
            if buf:
                joined = " ".join(buf).strip()
                if joined:
                    append_chunk(joined, protected=False)
                buf = []
                buf_len = 0
            for piece in _hard_split(sent, max_chars):
                append_chunk(piece, protected=True)
            continue

        added = len(sent) + (1 if buf else 0)
        if buf and buf_len + added > max_chars:
            joined = " ".join(buf).strip()
            if joined:
                append_chunk(joined, protected=False)
            if overlap_chars > 0 and joined:
                tail = joined[-overlap_chars:]
                buf = [tail]
                buf_len = len(tail)
            else:
                buf = []
                buf_len = 0
        buf.append(sent)
        buf_len += added

    if buf:
        joined = " ".join(buf).strip()
        if joined:
            append_chunk(joined, protected=False)

    out: list[str] = []
    for c, protected in zip(chunks, chunk_protected, strict=False):
        c = c.strip()
        if not c:
            continue
        if out and out[-1] == c and not protected:
            continue
        out.append(c)
    return out


# Back-compat: default embed model name (ollama tag)
DEFAULT_MODEL = os.environ.get("TOTAL_RECALL_EMBED_MODEL", RECOMMENDED_OLLAMA_EMBED)
