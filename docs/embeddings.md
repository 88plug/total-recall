# Dense embeddings (format v2) — product-owned ollama

Hybrid recall (FTS5 + dense RRF) embeds through **total-recall’s managed
ollama**, not an in-process ONNX stack and not “install ollama yourself” as the
primary path.

## Product model

| Piece | Who owns it |
|-------|-------------|
| Binary | `$CLAUDE_PLUGIN_DATA/total-recall/bin/ollama` (auto-fetched, no sudo) |
| Daemon | We start `ollama serve` on localhost when needed |
| Embed model | **`qwen3-embedding:0.6b`** (auto-pulled) |
| Chat model | **`qwen3.5:2b`** (optional refine; auto-pulled unless disabled) |

Hooks fire `recall::provision_llm` on first bootstrap. Rebuild / first embed
also call `vec.runtime.ensure_product_ollama` so CLI-only machines still work.

System PATH ollama is a **fallback** if present and GPU-capable; the product
still prefers the managed binary when it can.

## Two models

| Role | Tag | Required for |
|------|-----|----------------|
| Embed | `qwen3-embedding:0.6b` | Hybrid dense recall |
| Chat | `qwen3.5:2b` | LLM refine only — **not** an embedder |

## Zero-config path

```bash
# Usual: open Claude Code / Grok with the plugin installed — bootstrap
# provisions ollama + both models in the background.

# Manual repair / CLI-only:
bash scripts/llm-setup.sh
total-recall rebuild --yes
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
| `TOTAL_RECALL_EMBED_MODEL` | `qwen3-embedding:0.6b` | Ollama embed tag (not HF ids) |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://localhost:11434` | Product daemon URL |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Chat refine tag |
| `TOTAL_RECALL_LLM_PROVIDER` | `auto` | `none` disables **chat only** |
| `TOTAL_RECALL_VEC` | on | `0` skips dense (and embed pull) |
| `RECALL_OLLAMA` | (unset) | Force a specific ollama binary |

There is **no** `TOTAL_RECALL_EMBED_PROVIDER=fastembed`.

Full opt-out of product ollama: `TOTAL_RECALL_VEC=0` **and**
`TOTAL_RECALL_LLM_PROVIDER=none`.

## Hybrid fusion

Default **`dense_primary`**: dense rank order first, FTS only appends hits dense
missed. Stops weak keyword matches from stealing top-1 (eval fix: hybrid P@1
was 0.40 under equal RRF vs 0.80 pure dense).

| `TOTAL_RECALL_HYBRID_MODE` | Behaviour |
|----------------------------|-----------|
| `dense_primary` (default) | Dense order + FTS fill |
| `weighted_rrf` | RRF with dense weight 3× FTS (tunable) |
| `rrf` | Equal-weight RRF (legacy) |

## Rebuild when

- First dense setup
- You change `TOTAL_RECALL_EMBED_MODEL`
- Format v2 migration (pre-ollama / old indexes)

```bash
total-recall rebuild --yes
```

## CPU / GPU / MTP

Embed/LLM requests send **`num_gpu=999`** + **`keep_alive=-1`**. Product serve
also enables **MTP** (multi-token prediction) env for chat models that ship
`mtp.*` heads — default **`qwen3.5:2b`** does. Embeds are not MTP (not decode).

Optional host tuning: [ollama-gpu.md](ollama-gpu.md) (`scripts/ollama-gpu-hard.conf`).

## Privacy

Local product ollama only. No cloud embed APIs. Transcripts stay on the machine.
