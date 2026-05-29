---
name: total-recall:llm-setup
description: One-time setup for total-recall's optional local-LLM refinement layer — installs ollama (if missing) and pulls the configured model. Triggered when the SessionStart hook reports ollama / model missing.
---

# total-recall LLM setup

Runs the operator-facing setup script that installs ollama and pulls the configured
model (default `gemma4:e2b`). After it succeeds the refinement layer is active and
the SessionStart hook stops emitting the "not installed" notice.

Steps:

1. Run `${CLAUDE_PLUGIN_ROOT}/scripts/llm-setup.sh` and stream its output to the
   operator. The script is idempotent — re-running on a fully-set-up machine is
   a no-op.
2. When it exits 0, confirm to the operator that the refinement layer is enabled
   and recommend running `/total-recall:recall-rebuild` so the next rebuild
   picks up the LLM refinement passes.
3. If it exits non-zero, surface the error verbatim and point at the relevant
   line in `docs/install/llm-refinement.md`.
