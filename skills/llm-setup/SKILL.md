---
name: total-recall:llm-setup
description: One-time setup for total-recall's optional local-LLM refinement layer — installs ollama (if missing) and pulls the configured model. Triggered when the SessionStart hook reports ollama / model missing.
---

# total-recall LLM + embed setup

Runs the operator-facing setup script that installs ollama and pulls:

- refine model (default `qwen3.5:2b`) — optional LLM refinement layer
- embed model (default `qwen3-embedding:0.6b`) — format-v2 hybrid dense recall

After it succeeds, SessionStart stops emitting the "not installed" notice for ollama.

Steps:

1. Run `${CLAUDE_PLUGIN_ROOT}/scripts/llm-setup.sh` and stream its output to the
   operator. The script is idempotent — re-running on a fully-set-up machine is
   a no-op.
2. When it exits 0, confirm both models are ready and recommend
   `/total-recall:recall-rebuild` (or `total-recall rebuild --yes`) so the next
   rebuild picks up LLM refinement **and** dense vectors.
3. If the operator still has `TOTAL_RECALL_EMBED_MODEL` set to a HuggingFace id
   (e.g. `Alibaba-NLP/gte-modernbert-base`), tell them to **unset** it — embeds
   are ollama-only now.
4. If it exits non-zero, surface the error verbatim and point at
   `docs/llm-refinement.md` / `docs/embeddings.md`.
