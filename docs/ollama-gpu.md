# How Ollama uses the GPU (and how we hammer it)

## Mental model

Ollama is a **local model server**. You pull GGUF models; on each request it
loads weights into **VRAM (GPU)** and/or **RAM (CPU)** and runs inference.

| Concept | Meaning |
|---------|---------|
| **Same model tag** | No separate “GPU model” vs “CPU model.” Offload decides where layers live. |
| **`num_gpu`** | How many **layers** go to GPU (not “which GPU id”). Large value ≈ all layers. |
| **`100% GPU`** (`ollama ps`) | Entire model in VRAM — this is what you want. |
| **`48%/52% CPU/GPU`** | Split — model too big for free VRAM; slower. |
| **Multi-GPU** | If the model **fits one GPU**, ollama prefers that (best). If not, it spreads. |
| **`CUDA_VISIBLE_DEVICES`** | Which physical GPUs the daemon may see. |

Docs: [Hardware support](https://docs.ollama.com/gpu), [FAQ](https://docs.ollama.com/faq).

## total-recall defaults (code)

| Surface | Model | GPU knobs |
|---------|-------|-----------|
| Dense embed | `qwen3-embedding:0.6b` | `num_gpu=999`, `keep_alive=-1`, `num_ctx=8192`, `num_batch=512` |
| LLM refine | `qwen3.5:2b` | `num_gpu=999`, `keep_alive=-1` |

Env overrides:

- `TOTAL_RECALL_OLLAMA_NUM_GPU` (default `999`)
- `TOTAL_RECALL_EMBED_KEEP_ALIVE` / `TOTAL_RECALL_LLM_KEEP_ALIVE` (default `-1`)
- `TOTAL_RECALL_EMBED_NUM_CTX` (default `8192`)
- `TOTAL_RECALL_EMBED_NUM_BATCH` (default `512`)

## Daemon hardening (host)

Soft defaults on this box were: single GPU UUID, `OLLAMA_NUM_PARALLEL=1`,
`OLLAMA_KEEP_ALIVE=5m`. That **unloads** models and serializes work.

Hard profile (repo): `scripts/ollama-gpu-hard.conf` → install as systemd drop-in.

```bash
sudo cp scripts/ollama-gpu-hard.conf /etc/systemd/system/ollama.service.d/99-gpu-hard.conf
# Optional: remove older soft drop-in if it fights:
# sudo rm /etc/systemd/system/ollama.service.d/mosi.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
ollama pull qwen3-embedding:0.6b
ollama pull qwen3.5:2b
# Preload + pin
curl -s http://127.0.0.1:11434/api/embed -d '{"model":"qwen3-embedding:0.6b","input":"warmup","keep_alive":-1,"options":{"num_gpu":999}}' >/dev/null
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen3.5:2b","prompt":"hi","keep_alive":-1,"options":{"num_gpu":999,"num_predict":1}}' >/dev/null
ollama ps
nvidia-smi
```

Expect `PROCESSOR` → **100% GPU** for loaded models.

## Concurrent embed + chat

Both models small enough for one 20 GB Ada. With `OLLAMA_MAX_LOADED_MODELS≥2`
and enough free VRAM, both stay resident. If VRAM fights, ollama **evicts** the
idle model — longer `keep_alive` and smaller `num_ctx` on embeds reduce thrash.

## What “hammer” does *not* mean

- Not “use 8B embed always” (slower, more VRAM, rebuild cost).
- Not MTP (chat decode speedup; irrelevant to `/api/embed`).
- Not disabling safety on the host — only removing artificial throttles.
