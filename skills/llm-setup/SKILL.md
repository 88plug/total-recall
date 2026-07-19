---
name: total-recall:llm-setup
description: One-time setup for total-recall's product-owned ollama — installs the managed binary if needed and pulls default models. Triggered when SessionStart reports product ollama / models missing.
---

# total-recall LLM + embed setup

Runs the operator-facing setup script that installs product ollama (plugin data
dir, daemon on `:11435`) and pulls:

- refine model (default `qwen3.5:2b`) — chat refinement (provider default `auto`)
- embed model (default `qwen3-embedding:0.6b`) — format-v2 hybrid dense recall

**`TOTAL_RECALL_EMBED_MODEL` does not need to be set.** Unset is correct — the
plugin defaults to `qwen3-embedding:0.6b`. Do not set a HuggingFace id.

After setup succeeds and the product daemon is reachable with models present,
SessionStart stops emitting the not-ready notice (and will not re-fire once
shown once via `.ollama_notice_shown`).

Steps:

1. Run `${CLAUDE_PLUGIN_ROOT}/scripts/llm-setup.sh` and stream its output to the
   operator. The script is idempotent — re-running on a fully-set-up machine is
   a no-op. Leave `TOTAL_RECALL_EMBED_MODEL` unset unless overriding with another
   **ollama** tag.
2. When it exits 0, confirm both models are ready and recommend
   `/total-recall:recall-rebuild` (or `total-recall rebuild --yes`) so the next
   rebuild picks up LLM refinement **and** dense vectors.
3. **Only if** the operator still has a leftover HuggingFace/fastembed id in env
   (e.g. `TOTAL_RECALL_EMBED_MODEL=Alibaba-NLP/gte-modernbert-base` from pre-v2),
   tell them to **unset** it. Fresh installs never need this step. Product MCP
   config does not pin an embed model.
4. If it exits non-zero, surface the error verbatim and point at
   `docs/llm-refinement.md` / `docs/embeddings.md`.
