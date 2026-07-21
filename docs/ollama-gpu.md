# Ollama GPU notes (optional host tuning)

**Default product path:** total-recall auto-provisions a managed ollama binary
under the plugin data dir and starts it — no systemd required. See
[embeddings.md](embeddings.md).

This page is **optional** host-level tuning when you already run a system
ollama unit and want hard GPU pin.

## What “100% GPU” means

| Signal | Meaning |
|--------|---------|
| **`100% GPU`** (`ollama ps`) | Entire model in VRAM |
| **Multi-GPU** | Fits one GPU → prefer that; else spread |
| **`CUDA_VISIBLE_DEVICES`** | Which physical GPUs the daemon may see |

Docs: [Hardware support](https://docs.ollama.com/gpu), [FAQ](https://docs.ollama.com/faq).

## What total-recall already sends

| Role | Model | Request options |
|------|-------|-----------------|
| Dense embed | `qwen3-embedding:0.6b` | `num_gpu=999`, `keep_alive=-1`, `num_ctx=8192`, `num_batch=512` |
| Chat refine | `qwen3.5:2b` | `num_gpu=999`, `keep_alive=-1` (+ model-native **MTP**) |

Env overrides: `TOTAL_RECALL_OLLAMA_NUM_GPU`, `TOTAL_RECALL_EMBED_KEEP_ALIVE`,
`TOTAL_RECALL_EMBED_NUM_CTX`, `TOTAL_RECALL_EMBED_NUM_BATCH`.

## Multi-token prediction (MTP)

| Model | MTP? | How |
|-------|------|-----|
| **`qwen3.5:2b`** (default chat) | **Yes** | Built-in `mtp.*` tensors in the GGUF; ollama CUDA/llama-server auto-engages |
| **`qwen3-embedding:*`** | N/A | Embed path is not autoregressive decode |
| Gemma 4 library tags with `-mtp` | Yes | Pull the MTP-tagged model (separate drafter) |
| Other chat models | Only if GGUF ships MTP heads or a draft model is paired |

Product `ollama serve` (hooks + `vec.runtime`) always sets:

- `OLLAMA_FLASH_ATTENTION=1`
- `OLLAMA_MLX_MTP_MAX_DRAFT_TOKENS=4`
- `OLLAMA_MLX_MTP_INITIAL_DRAFT_TOKENS=4`
- `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=4`, `OLLAMA_NUM_PARALLEL=16`
  (16 slots for concurrent embed batches during rebuild backfill)

MLX runners honor the `OLLAMA_MLX_MTP_*` knobs. CUDA runners use built-in heads
on Qwen3.5 (no separate draft model required).

## Optional: system systemd drop-in

Only if you prefer a **system** `ollama.service` over the product-managed binary:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp scripts/ollama-gpu-hard.conf /etc/systemd/system/ollama.service.d/99-gpu-hard.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
# Point product at that daemon (default URL already matches):
# export TOTAL_RECALL_LLM_BASE_URL=http://127.0.0.1:11435
```

Or pin the product binary: `export RECALL_OLLAMA=/usr/bin/ollama`.

Verify:

```bash
ollama ps          # PROCESSOR → 100% GPU
```

## Concurrent embed + chat

With `OLLAMA_MAX_LOADED_MODELS` ≥ 2 and free VRAM, both stay resident. If VRAM
fights, ollama may evict the idle model — long `keep_alive` helps.

## Not required

- Not “install ollama yourself first” for normal plugin users
- Not “use 8B embed always”
- Not MTP (chat decode; irrelevant to `/api/embed`)
