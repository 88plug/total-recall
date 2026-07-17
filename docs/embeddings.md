# Dense embeddings (format v2)

Hybrid recall (FTS5 + dense RRF) embeds text through the **local ollama**
daemon by default — the same stack as [LLM refinement](llm-refinement.md).

## Default path

| Role | Default |
|------|---------|
| Backend | ollama (`TOTAL_RECALL_EMBED_PROVIDER=ollama`) |
| Model | **`qwen3-embedding:0.6b`** (1024-d, 32K ctx) |
| LLM refine (separate) | `qwen3.5:2b` — chat only, not an embedder |

1. Ensure ollama is running.
2. Pull the default embed model:

   ```bash
   ollama pull qwen3-embedding:0.6b
   ```

3. Rebuild the dense index (required when upgrading from pre-v2 / fastembed-only):

   ```bash
   total-recall rebuild --yes
   # or:
   python -m vec.cli rebuild
   python -m vec.cli backfill
   ```

Old indexes without `vec_meta.format=2` refuse to load — no silent mix of
embedding spaces.

## Query vs document (qwen3-embedding)

Queries use the official instruct form (applied when searching):

```text
Instruct: Given a web search query, retrieve relevant passages that answer the query
Query:{your query}
```

Indexed chunks (documents) are embedded as **raw text** (no prefix).

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `TOTAL_RECALL_EMBED_PROVIDER` | `ollama` | `ollama` · `auto` (ollama then fastembed) · `fastembed` (CPU ONNX escape hatch) |
| `TOTAL_RECALL_EMBED_MODEL` | (auto) | Prefer `qwen3-embedding:0.6b` if pulled; else smallest embedding-capable tag |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://localhost:11434` | Shared daemon URL (LLM + embed) |
| `TOTAL_RECALL_VEC` | on | Set `0` to skip dense backfill on rebuild |

Optional larger tags: `qwen3-embedding:4b`, `:8b` — set `TOTAL_RECALL_EMBED_MODEL` and
**rebuild** (dim/model identity is locked in `vec_meta`).

## CPU vs GPU

Same model tag always. Ollama offloads layers to GPU when VRAM allows; on CPU-only
hosts the same `qwen3-embedding:0.6b` runs slower. There is no separate “CPU model.”

LLM refine (`qwen3.5:2b`) and embed (`qwen3-embedding:0.6b`) share one daemon and
VRAM pool — both typically fit on a modest GPU.

## Escape hatch

CI and air-gapped boxes without ollama:

```bash
export TOTAL_RECALL_EMBED_PROVIDER=fastembed
```

Uses `BAAI/bge-small-en-v1.5` (384-d) in-process. Product default remains ollama.

## Privacy

Transcripts never leave the machine. Embed requests go only to the local ollama
HTTP API (or in-process fastembed if forced). No cloud embed providers.
