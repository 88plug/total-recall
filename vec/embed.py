"""Embedding wrapper — **ollama by default**, fastembed as explicit escape hatch.

Product contract (format v2, 2026-07):
  * Default backend is **ollama** (same local daemon as LLM refinement).
  * Dense vectors are ollama-native; old fastembed-only indexes must rebuild.
  * ``TOTAL_RECALL_EMBED_PROVIDER``: ``ollama`` (default) | ``auto`` | ``fastembed``.
      - ``ollama`` — require daemon + embedding-capable model (fail loud).
      - ``auto`` — ollama if available, else fastembed (CI / no-daemon machines).
      - ``fastembed`` — CPU ONNX path (legacy escape hatch; not the product default).
  * ``TOTAL_RECALL_EMBED_MODEL`` — ollama tag (default pick ``qwen3-embedding:0.6b``)
    or HF id when provider=fastembed / explicit ``Embedder(model=…)``.
  * ``TOTAL_RECALL_LLM_BASE_URL`` — reused for the daemon URL (no separate var).

``Embedder(model="BAAI/…")`` always forces fastembed so the existing unit suite
and pinned evals stay hermetic.

HTTP: ``POST /api/embed`` with ``input`` → ``embeddings`` (stdlib only; no pip ollama).

Qwen3-embedding query convention (as_query=True):
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
from pathlib import Path
from typing import Any

_INSTALL_HINT = (
    "fastembed is not installed. Install core total-recall deps, or set "
    "TOTAL_RECALL_EMBED_PROVIDER=ollama with a running ollama daemon:\n"
    "    pip install 'total-recall'\n"
    "    ollama pull qwen3-embedding:0.6b"
)

# Legacy HF default — only used when provider=fastembed or Embedder(model=…).
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
# Product default ollama embedder (quality + modest size; already common on GPU boxes).
# Module never auto-pulls — operator must `ollama pull` once.
RECOMMENDED_OLLAMA_EMBED = "qwen3-embedding:0.6b"

# Official Qwen3-Embedding retrieval instruction (English task string).
QWEN3_QUERY_INSTRUCT = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
    "Query:"
)

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
_OLLAMA_PROBE_TIMEOUT_S = 10.0
_OLLAMA_EMBED_TIMEOUT_S = 120.0  # cold load can hang >60s

# Known dimensions for models we explicitly support. Other models discover
# dim on first `.embed()` call.
_KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "nomic-embed-text-v1.5": 768,
    "Alibaba-NLP/gte-modernbert-base": 768,
    "onnx-community/granite-embedding-small-english-r2": 384,
    # Common ollama tags (live-verified dims where noted):
    "granite-embedding:30m": 384,
    "granite-embedding:278m": 768,
    "embeddinggemma:300m": 768,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "nomic-embed-text-v2-moe:latest": 768,
    "qwen3-embedding:0.6b": 1024,
    "qwen3-embedding:4b": 2560,
    "qwen3-embedding:8b": 4096,
    "qwen3-embedding:latest": 4096,  # ollama :latest → 8b
    "qwen3-embedding": 4096,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}

_CUSTOM_MODELS: dict[str, dict] = {
    "Alibaba-NLP/gte-modernbert-base": {
        "pooling": "CLS",
        "normalization": True,
        "hf": "Alibaba-NLP/gte-modernbert-base",
        "model_file": "onnx/model.onnx",
        "additional_files": None,
        "dim": 768,
    },
    "onnx-community/granite-embedding-small-english-r2": {
        "pooling": "CLS",
        "normalization": True,
        "hf": "onnx-community/granite-embedding-small-english-r2-ONNX",
        "model_file": "onnx/model.onnx",
        "additional_files": ["onnx/model.onnx_data"],
        "dim": 384,
    },
}

# Query instruction prefixes (only when embed(..., as_query=True)).
_QUERY_PREFIX: dict[str, str] = {
    "BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "nomic-embed-text": "search_query: ",
    "nomic-embed-text:latest": "search_query: ",
    "nomic-ai/nomic-embed-text-v1.5": "search_query: ",
    "nomic-embed-text-v1.5": "search_query: ",
    "qwen3-embedding:0.6b": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding:4b": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding:8b": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding:latest": QWEN3_QUERY_INSTRUCT,
    "qwen3-embedding": QWEN3_QUERY_INSTRUCT,
}

_DOC_PREFIX: dict[str, str] = {
    "nomic-embed-text": "search_document: ",
    "nomic-embed-text:latest": "search_document: ",
    "nomic-ai/nomic-embed-text-v1.5": "search_document: ",
    "nomic-embed-text-v1.5": "search_document: ",
    # qwen3-embedding: documents are raw (no prefix)
}


def _query_prefix_for(model: str) -> str:
    if model in _QUERY_PREFIX:
        return _QUERY_PREFIX[model]
    if model.startswith("qwen3-embedding"):
        return QWEN3_QUERY_INSTRUCT
    return ""


def _doc_prefix_for(model: str) -> str:
    if model in _DOC_PREFIX:
        return _DOC_PREFIX[model]
    return ""


def _clamp_tokenizer_max_length(model_dir) -> None:
    import json as _json
    from pathlib import Path as _Path

    tc = _Path(model_dir) / "tokenizer_config.json"
    if not tc.exists():
        return
    try:
        d = _json.loads(tc.read_text())
    except Exception:
        return
    changed = False
    for k in ("model_max_length", "max_length"):
        v = d.get(k)
        if isinstance(v, int) and v > 1_000_000:
            d[k] = 8192
            changed = True
    if changed:
        tc.write_text(_json.dumps(d))


def _install_tokenizer_clamp() -> None:
    try:
        from fastembed.text import onnx_text_model as _otm
    except Exception:
        return
    if getattr(_otm, "_tr_maxlen_clamp", False):
        return
    _orig = getattr(_otm, "load_tokenizer", None)
    if _orig is None:
        return

    def _wrapped(*args, **kwargs):
        md = kwargs.get("model_dir")
        if md is None and args:
            md = args[0]
        if md is not None:
            _clamp_tokenizer_max_length(md)
        return _orig(*args, **kwargs)

    _otm.load_tokenizer = _wrapped  # type: ignore[attr-defined]
    _otm._tr_maxlen_clamp = True  # type: ignore[attr-defined]


def _register_custom_model(model: str) -> None:
    spec = _CUSTOM_MODELS.get(model)
    if not spec:
        return
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    try:
        TextEmbedding.add_custom_model(
            model=model,
            dim=spec["dim"],
            pooling=PoolingType[spec["pooling"]],
            normalization=spec["normalization"],
            sources=ModelSource(hf=spec["hf"]),
            model_file=spec["model_file"],
            additional_files=spec["additional_files"],
        )
    except ValueError as exc:
        if "already" not in str(exc).lower():
            raise


# ----------------------------------------------------------------------------
# ollama backend
# ----------------------------------------------------------------------------


def _ollama_base_url() -> str:
    return (os.environ.get("TOTAL_RECALL_LLM_BASE_URL") or _OLLAMA_DEFAULT_BASE_URL).rstrip("/")


def _ollama_list_models(base_url: str) -> list[dict[str, Any]] | None:
    """GET /api/tags. Returns models list, or None if unreachable."""
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


def _pick_ollama_embed_model(base_url: str, want: str | None) -> str | None:
    """Return an embedding-capable pulled model name, or None."""
    models = _ollama_list_models(base_url)
    if not models:
        return None
    embedding_capable = [
        m for m in models if isinstance(m, dict) and _is_embedding_model(m)
    ]
    if not embedding_capable:
        return None

    if want:
        want_latest = want if ":" in want else f"{want}:latest"
        names = {m.get("name", "") for m in embedding_capable}
        if want in names:
            return want
        if want_latest in names:
            return want_latest

    # Prefer product default (qwen3-embedding:0.6b), then any 0.6b quant tag,
    # else smallest embedding-capable model by size.
    names = {str(m.get("name") or "") for m in embedding_capable}
    if RECOMMENDED_OLLAMA_EMBED in names:
        return RECOMMENDED_OLLAMA_EMBED
    for name in sorted(names):
        if name.startswith("qwen3-embedding:0.6b"):
            return name

    embedding_capable.sort(key=lambda m: m.get("size", 0) or 0)
    return embedding_capable[0].get("name") or None


def _ollama_embed(base_url: str, model: str, texts: list[str]) -> list[list[float]]:
    """POST /api/embed. Raises RuntimeError on failure."""
    payload = {"model": model, "input": texts, "truncate": False}
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


def _provider_env() -> str:
    return (os.environ.get("TOTAL_RECALL_EMBED_PROVIDER") or "ollama").strip().lower()


class Embedder:
    """Lazy embedding wrapper — ollama by default, fastembed escape hatch.

    Args:
        model: If set, forces the **fastembed** backend with this HF-style id
            (tests + pinned evals). If None, resolve via env (product default:
            ollama).
        cache_dir: fastembed cache only; ignored by ollama.
    """

    def __init__(self, model: str | None = None, cache_dir: Path | None = None) -> None:
        self._explicit_model = model
        self.model = model or ""
        self.cache_dir = cache_dir
        self._impl = None
        self._backend: str | None = None
        self._ollama_base_url: str | None = None
        self._dim: int | None = _KNOWN_DIMS.get(model) if model else None
        self._query_prefix: str = _query_prefix_for(model) if model else ""
        self._doc_prefix: str = _doc_prefix_for(model) if model else ""

    def _load(self) -> None:
        if self._backend is not None:
            return

        if self._explicit_model is not None:
            self._load_fastembed(self._explicit_model)
            return

        provider = _provider_env()
        if provider not in ("ollama", "auto", "fastembed"):
            raise RuntimeError(
                f"TOTAL_RECALL_EMBED_PROVIDER={provider!r} invalid; "
                f"use ollama | auto | fastembed"
            )

        if provider == "fastembed":
            want = os.environ.get("TOTAL_RECALL_EMBED_MODEL") or DEFAULT_FASTEMBED_MODEL
            self._load_fastembed(want)
            return

        # ollama (default) or auto
        base_url = _ollama_base_url()
        want = os.environ.get("TOTAL_RECALL_EMBED_MODEL")
        chosen = _pick_ollama_embed_model(base_url, want)
        if chosen is not None:
            self._load_ollama(chosen, base_url)
            return

        if provider == "ollama":
            raise RuntimeError(
                f"TOTAL_RECALL_EMBED_PROVIDER=ollama but no embedding-capable model "
                f"is reachable at {base_url}. Pull one, e.g.:\n"
                f"    ollama pull {RECOMMENDED_OLLAMA_EMBED}\n"
                f"Or set TOTAL_RECALL_EMBED_PROVIDER=auto for fastembed fallback, "
                f"or =fastembed to force CPU ONNX."
            )
        # auto → fastembed
        want_fe = os.environ.get("TOTAL_RECALL_EMBED_MODEL") or DEFAULT_FASTEMBED_MODEL
        # If EMBED_MODEL was an ollama tag, ignore it for fastembed path
        if want_fe and ":" in want_fe and not want_fe.startswith("BAAI/") and "/" not in want_fe:
            want_fe = DEFAULT_FASTEMBED_MODEL
        self._load_fastembed(want_fe)

    def _load_fastembed(self, model: str) -> None:
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(_INSTALL_HINT) from exc

        _install_tokenizer_clamp()
        _register_custom_model(model)

        kwargs: dict[str, object] = {"model_name": model}
        if self.cache_dir is not None:
            kwargs["cache_dir"] = str(self.cache_dir)
        self._impl = TextEmbedding(**kwargs)  # type: ignore[arg-type]
        self._backend = "fastembed"
        self.model = model
        if self._dim is None:
            self._dim = _KNOWN_DIMS.get(model)
        self._query_prefix = _query_prefix_for(model)
        self._doc_prefix = _doc_prefix_for(model)

    def _load_ollama(self, model: str, base_url: str) -> None:
        self._backend = "ollama"
        self.model = model
        self._ollama_base_url = base_url
        self._query_prefix = _query_prefix_for(model)
        self._doc_prefix = _doc_prefix_for(model)
        if self._dim is None:
            self._dim = _KNOWN_DIMS.get(model)
            # Prefer live embedding_length from tags when known
            models = _ollama_list_models(base_url) or []
            for m in models:
                if isinstance(m, dict) and m.get("name") == model:
                    hint = _embedding_dim_hint(m)
                    if hint:
                        self._dim = hint
                    break

    def embed(self, texts: list[str], as_query: bool = False) -> list[list[float]]:
        """Embed a batch of texts. Returns list of float vectors in input order."""
        if not texts:
            return []
        self._load()

        if as_query and self._query_prefix:
            texts = [self._query_prefix + t for t in texts]
        elif not as_query and self._doc_prefix:
            texts = [self._doc_prefix + t for t in texts]

        if self._backend == "ollama":
            assert self._ollama_base_url is not None
            out = _ollama_embed(self._ollama_base_url, self.model, texts)
        else:
            assert self._impl is not None
            out = []
            for vec in self._impl.embed(texts):  # type: ignore[attr-defined]
                tolist = getattr(vec, "tolist", None)
                row = tolist() if callable(tolist) else list(vec)
                out.append([float(x) for x in row])

        if self._dim is None and out:
            self._dim = len(out[0])
        return out

    def dim(self) -> int:
        if self._dim is not None:
            return self._dim
        # Probe
        self.embed(["."])
        if self._dim is None:
            raise RuntimeError("could not discover embedding dim")
        return self._dim

    @property
    def backend(self) -> str | None:
        """``ollama`` | ``fastembed`` after first load; None before."""
        if self._backend is None and self._explicit_model is None:
            # Resolve without embedding if possible for meta writes
            try:
                self._load()
            except Exception:  # noqa: BLE001
                return None
        return self._backend

    def identity(self) -> str:
        """Stable index identity string: ``backend:model``."""
        self._load()
        return f"{self._backend}:{self.model}"


# ----------------------------------------------------------------------------
# Chunking (backend-agnostic)
# ----------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


_PARA_RE = re.compile(r"\n\s*\n")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def _split_sentences(text: str) -> list[str]:
    parts = _PARA_RE.split(text)
    out: list[str] = []
    for para in parts:
        para = para.strip()
        if not para:
            continue
        sents = _SENT_RE.split(para)
        for s in sents:
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

    def append_chunk(text: str, protected: bool) -> None:
        chunks.append(text)
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


# Back-compat alias used by older code/tests
DEFAULT_MODEL = os.environ.get("TOTAL_RECALL_EMBED_MODEL", DEFAULT_FASTEMBED_MODEL)
