"""Embedding wrapper around `fastembed`.

Design rules:
  * `fastembed` is **only** imported inside `Embedder.__init__` /
    `Embedder.embed`. Importing this module must NOT pull `fastembed` —
    users without the `vec` extra still get the rest of `total-recall`.
  * Default model is `BAAI/bge-small-en-v1.5` (384-dim, fast, good baseline).
  * The env var `TOTAL_RECALL_EMBED_MODEL` overrides the default — useful
    for the long-context escape hatch `nomic-embed-text-v1.5` (768-dim).
  * `chunk_for_embedding` is sentence-aware: it splits on `". "` and `"\\n\\n"`
    boundaries first and only falls back to a hard length split when a single
    sentence exceeds the budget.

If the embedding dim changes from what the on-disk vec index was built with,
`store.vec_search` / `store.upsert_extraction_embedding` will surface a clear
"`vec rebuild` required" error.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_INSTALL_HINT = (
    "fastembed is not installed. Install the optional 'vec' extra:\n"
    "    pip install 'total-recall[vec]'"
)

DEFAULT_MODEL = os.environ.get("TOTAL_RECALL_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# Known dimensions for models we explicitly support. Other models will have
# their dim discovered on first `.embed()` call.
_KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "nomic-ai/nomic-embed-text-v1.5": 768,
    # Allow the short alias the user might pass via env:
    "nomic-embed-text-v1.5": 768,
    # Custom (non-builtin) models auto-registered on first use (see _CUSTOM_MODELS):
    "Alibaba-NLP/gte-modernbert-base": 768,
    "onnx-community/granite-embedding-small-english-r2": 384,
}


# Non-builtin fastembed models we auto-register via add_custom_model on first use.
# Configs verified from each model card: pooling, normalization, no query prompt.
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


def _clamp_tokenizer_max_length(model_dir) -> None:
    """Some models (e.g. ModernBERT) ship model_max_length as a huge sentinel
    (~1e30) that overflows the Rust tokenizer's enable_truncation. Rewrite it to
    a sane cap in the cached tokenizer_config.json before fastembed reads it.
    Idempotent; re-applied on next load if a re-download reverts it."""
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
    """Monkeypatch fastembed load_tokenizer to sanitize model_max_length first."""
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


# Models that benefit from an explicit query instruction prefix. bge-class
# models gain ~+2.3pp R@10 from this; gte-modernbert / granite / nomic are
# deliberately absent so they get NO prefix (a no-op). Only applied when a
# caller opts in via Embedder.embed(..., as_query=True).
_QUERY_PREFIX: dict[str, str] = {
    "BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
}


class Embedder:
    """Lazy fastembed wrapper. Constructing this is cheap; the model is loaded
    on first `.embed()` call so test imports and `--help` are fast.

    Args:
        model: HF-style model id. Defaults to `BAAI/bge-small-en-v1.5`.
        cache_dir: Optional override for fastembed's model cache.
    """

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Path | None = None) -> None:
        self.model = model
        self.cache_dir = cache_dir
        self._impl = None  # fastembed.TextEmbedding, lazy
        self._dim: int | None = _KNOWN_DIMS.get(model)
        self._query_prefix: str = _QUERY_PREFIX.get(model, "")

    # ------------------------------------------------------------------ impl

    def _load(self) -> None:
        if self._impl is not None:
            return
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only without extra
            raise RuntimeError(_INSTALL_HINT) from exc

        _install_tokenizer_clamp()
        _register_custom_model(self.model)

        kwargs: dict[str, object] = {"model_name": self.model}
        if self.cache_dir is not None:
            kwargs["cache_dir"] = str(self.cache_dir)
        self._impl = TextEmbedding(**kwargs)  # type: ignore[arg-type]

    def embed(self, texts: list[str], as_query: bool = False) -> list[list[float]]:
        """Embed a batch of texts. Returns a list of float vectors in input order.

        When ``as_query=True`` and this model has an entry in ``_QUERY_PREFIX``
        (bge-class only), each text is prefixed with the model's query
        instruction. Defaults to ``False`` (passage embedding) so all existing
        callers are byte-identical.
        """
        if not texts:
            return []
        if as_query and self._query_prefix:
            texts = [self._query_prefix + t for t in texts]
        self._load()
        assert self._impl is not None
        # fastembed yields numpy arrays; convert to plain python lists so the
        # caller doesn't need numpy on the import path.
        out: list[list[float]] = []
        embedded: Any = self._impl.embed(texts)  # type: ignore[attr-defined]
        for vec in embedded:
            # vec is a np.ndarray; .tolist() works for either ndarray or list.
            tolist = getattr(vec, "tolist", None)
            if callable(tolist):
                row_any: Any = tolist()
            else:
                row_any = list(vec)
            row = [float(x) for x in row_any]
            out.append(row)
            if self._dim is None and row:
                self._dim = len(row)
        return out

    def dim(self) -> int:
        """Return the embedding dimensionality.

        For known models this is available without loading anything. For unknown
        models the dim is discovered by embedding a single short probe string.
        """
        if self._dim is not None:
            return self._dim
        # Force one embedding call to discover dim.
        vecs = self.embed(["probe"])
        if not vecs:
            raise RuntimeError("Embedder produced no output for probe input")
        self._dim = len(vecs[0])
        return self._dim


# ----------------------------------------------------------------------------
# Chunking
# ----------------------------------------------------------------------------

# Rough char-per-token heuristic for the bge-small / nomic family. We avoid a
# hard tokenizer dependency on this path so chunking works without fastembed
# installed (used by tests and by the FTS5-only baseline if it ever wants
# parallel chunks).
_CHARS_PER_TOKEN = 4


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


# Split on paragraph breaks first, then on sentence terminators followed by
# whitespace. We keep the terminator on the left half so the chunk reads
# naturally.
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
    """Last-resort splitter for sentences that exceed the per-chunk budget."""
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def chunk_for_embedding(text: str, max_tokens: int = 400, overlap: int = 50) -> list[str]:
    """Split `text` into embedding-sized chunks.

    Behaviour:
      * Sentence-aware: prefers `". "` and `"\\n\\n"` boundaries.
      * Greedy: packs sentences into a chunk until the next one would exceed
        `max_tokens`; emits the chunk and starts a new one carrying `overlap`
        tokens of trailing context.
      * Hard-split fallback: if a single sentence exceeds the budget, it gets
        chopped on character count.

    Returns an empty list for empty / whitespace-only input.
    """
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

    # We tag chunks that came from a hard-split run so the post-pass doesn't
    # collapse legitimately-repeated content (e.g. uniform AAAA...).
    chunk_protected: list[bool] = []

    def append_chunk(text: str, protected: bool) -> None:
        chunks.append(text)
        chunk_protected.append(protected)

    for sent in sentences:
        # If even the sentence alone busts the budget, hard-split it.
        if len(sent) > max_chars:
            # Drain any buffered prefix as its own chunk first (without
            # producing an overlap that would corrupt the hard-split sequence).
            if buf:
                joined = " ".join(buf).strip()
                if joined:
                    append_chunk(joined, protected=False)
                buf = []
                buf_len = 0
            for piece in _hard_split(sent, max_chars):
                append_chunk(piece, protected=True)
            continue

        # +1 for the joining space.
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

    # Final flush.
    if buf:
        joined = " ".join(buf).strip()
        if joined:
            append_chunk(joined, protected=False)

    # Strip empty residue and dedupe consecutive identical chunks (overlap can
    # produce a tail==chunk duplicate on very short inputs). Protected chunks
    # — products of the hard-splitter — are never deduped against each other.
    out: list[str] = []
    for c, protected in zip(chunks, chunk_protected, strict=False):
        c = c.strip()
        if not c:
            continue
        if out and out[-1] == c and not protected:
            continue
        out.append(c)
    return out
