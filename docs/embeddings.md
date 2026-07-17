# Dense embeddings (format v2) — ollama only

Hybrid recall (FTS5 + dense RRF) embeds **only** through the local ollama
daemon. There is no fastembed / ONNX path.

## Two models (minimum)

| Role | Tag | Job |
|------|-----|-----|
| Embed | **`qwen3-embedding:0.6b`** | Dense vectors (1024-d) |
| Chat | **`qwen3.5:2b`** | LLM refine only — **not** an embedder |

```bash
ollama pull qwen3-embedding:0.6b
ollama pull qwen3.5:2b
total-recall rebuild --yes   # first time / after model change
```

## Query vs document (qwen3-embedding)

- **Queries** (search): official instruct form (applied by total-recall):

  ```text
  Instruct: Given a web search query, retrieve relevant passages that answer the query
  Query:{your query}
  ```

- **Documents** (index): raw text, no prefix.

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `TOTAL_RECALL_EMBED_MODEL` | auto → `qwen3-embedding:0.6b` if pulled | Ollama embedding tag |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://localhost:11434` | Shared daemon |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Refine (chat) |
| `TOTAL_RECALL_VEC` | on | `0` skips dense backfill |

There is **no** `TOTAL_RECALL_EMBED_PROVIDER=fastembed`.

## Rebuild when

- First successful dense setup
- You change `TOTAL_RECALL_EMBED_MODEL`
- Format v2 migration (pre-ollama / old indexes)

```bash
python -m vec.cli rebuild
python -m vec.cli backfill
# or
total-recall rebuild --yes
```

## CPU / GPU

Same tags. Ollama offloads when VRAM allows. CPU is correct, just slower.

## Privacy

Local ollama only. No cloud embed APIs. Transcripts stay on the machine.
