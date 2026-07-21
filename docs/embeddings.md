# Dense embeddings (format v2) — product-owned ollama

Hybrid recall (FTS5 + dense RRF) embeds through **total-recall’s managed
ollama**, not an in-process ONNX stack and not “install ollama yourself” as the
primary path.

## Product model

| Piece | Who owns it |
|-------|-------------|
| Binary | `$CLAUDE_PLUGIN_DATA/total-recall/bin/ollama` (**auto-updated** to latest, no sudo) |
| Daemon | Product-owned **`127.0.0.1:11435`** only (never rides system `:11434`) |
| Embed model | **`qwen3-embedding:0.6b`** (auto-pulled) |
| Chat model | **`qwen3.5:2b`** (optional refine; auto-pulled unless disabled) |

Hooks fire `recall::provision_llm` on first bootstrap. Rebuild / first embed
also call `vec.runtime.ensure_product_ollama` so CLI-only machines still work.

**Binary auto-update:** product-embedded only (PATH/snap never used unless
`RECALL_OLLAMA_ALLOW_SYSTEM=1`). On resolve, probes GitHub latest (24h TTL) and
re-fetches when behind. Current Linux package is `.tar.zst` (CUDA libs; needs
`zstd`). Pin with `OLLAMA_VERSION=0.32.1`. Disable bumps with
`RECALL_OLLAMA_AUTO_UPDATE=0` (still installs if missing).

**Daemon ownership:** default URL is `http://127.0.0.1:11435`. Serve / pull /
embed / refine only count when the **product binary** is the process bound to
that URL (`OLLAMA_HOST` set on start). A live system ollama on `:11434` is
ignored — no version skew, no silent foreign daemon.

Why latest matters: **ollama ≥0.31.2** fixed *structured output for thinking
models when thinking is disabled* — exactly our qwen3.5 `think:false` + JSON
schema refine path.

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

Card rules (Qwen3-Embedding-0.6B): **last-token pool**, **L2 normalize**, **cosine**,
instruct on **queries only**, documents raw, English task text.

**sqlite-vec index metric:** `vec_chunks` is created as
`vec0(embedding float[DIM] distance_metric=cosine)` (sqlite-vec **0.1.9** column
option). Default vec0 float metric is **L2** — wrong for the card without an
explicit cosine pin. After L2-normalize, ranks are similar, but cosine is the
correct distance space (and required for non-unit edge cases).

- **Queries** (search) — product **domain** instruct (not generic web search).
  Card template + session-memory task (live A/B winner vs long laundry-list):

  ```text
  Instruct: Given a query, retrieve relevant past engineering session passages that answer the query
  Query:{your query}
  ```

  Note: **no space** after `Query:` (card format). Generic web-search instruct
  via `TOTAL_RECALL_EMBED_INSTRUCT=web`; prior memory line via `memory_v1`.
  HF: custom English instructs help ~1–5% vs no instruct.

- **Documents** (index): raw text, no prefix. Changing query instruct does
  **not** require re-embed (docs never get the prefix).

- API knobs total-recall sets: `truncate=false`, `num_ctx=8192`, `keep_alive=-1`,
  client L2 normalize if the backend is non-unit.

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `TOTAL_RECALL_EMBED_MODEL` | `qwen3-embedding:0.6b` | Ollama embed tag. **Leave unset** for the default — no HF id is required or used. |
| `TOTAL_RECALL_EMBED_INSTRUCT` | product memory task | `web` / `memory` / `memory_v1` / full `Instruct:…\nQuery:` / bare task |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://127.0.0.1:11435` | Product daemon URL (not system 11434) |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Chat refine tag |
| `TOTAL_RECALL_LLM_PROVIDER` | `auto` | `none` disables **chat only** |
| `TOTAL_RECALL_VEC` | on | `0` skips dense (and embed pull) |
| `TOTAL_RECALL_EMBED_BATCH` | `256` | Extractions per outer backfill round |
| `TOTAL_RECALL_EMBED_MAX_INPUT` | `128` | Max texts per `/api/embed` call |
| `TOTAL_RECALL_EMBED_CONCURRENCY` | `4` | Parallel embed HTTP calls (fills `OLLAMA_NUM_PARALLEL`) |
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
