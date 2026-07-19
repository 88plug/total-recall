# Local-LLM refinement

total-recall is 100% deterministic heuristics by default — zero model calls,
zero network egress. The local-LLM refinement layer adds a second pass that
improves machine-name extraction accuracy, vocabulary definitions, and
per-project narrative summaries.

## Auto-setup (zero config)

On first install the plugin bootstrap automatically:

1. Downloads the ollama binary (~38 MB, no `sudo`, into the plugin data dir).
2. Pulls the default model `qwen3.5:2b` (~2.7 GB).
3. Starts a localhost ollama daemon.

All of this runs detached in the background — the spawning Claude Code session
does not wait for it and does not need to stay open. A one-time banner in your
first session announces that setup is in progress.

Nothing for you to do. Refinement activates automatically once the model is
ready, on the next `rebuild`.

## Privacy

**Your transcripts never leave the machine.** The model runs on-device via
ollama. Cloud APIs are deliberately not supported — they would break the
no-reupload guarantee that is a core design constraint of total-recall. The
product ollama daemon listens on `127.0.0.1:11435` by default (never system `:11434`).

## What refinement improves

Refinement runs on the cold path only (during `total-recall index --rebuild`).
The heuristic baseline is always the fallback; refinement is additive only.

| What | Heuristic baseline | With qwen3.5:2b |
|---|---|---|
| Machine-name extraction | Pattern-based NER | Precision 1.0, Recall 1.0 |
| Vocabulary definitions | Terms listed, no definitions | ~60% define coverage |
| Project narratives | Not generated | Short accurate summaries |
| Runtime (machines pass) | ~3s | ~19s |

These numbers are from a head-to-head eval against `gemma4:e2b` (definition
coverage ~0.20) and `qwen3.5:4b`. `qwen3.5:2b` won on all three axes at the
2B size; larger models can improve definition coverage further.

## Env vars

| Env var | Default | Effect |
|---|---|---|
| `TOTAL_RECALL_LLM_PROVIDER` | `auto` | `none` disables the entire LLM layer (no download, no daemon, no refinement). `ollama` forces the ollama code path. |
| `TOTAL_RECALL_LLM_MODEL` | `qwen3.5:2b` | Model tag to use. Any model you have pulled with `ollama pull` works. |
| `TOTAL_RECALL_LLM_REFINE_TEXT` | `1` | Set to `0` to disable vocab/narrative refinement while keeping machine-name extraction. |
| `TOTAL_RECALL_LLM_BASE_URL` | `http://127.0.0.1:11435` | Product ollama API (not system 11434). |

### Disable entirely

```bash
export TOTAL_RECALL_LLM_PROVIDER=none
```

No download, no daemon, no refinement — pure heuristics, same as v0.8 and earlier.

### Use a larger model

```bash
export TOTAL_RECALL_LLM_MODEL=qwen3.5:4b   # more RAM, slower, higher coverage
ollama pull qwen3.5:4b
total-recall index --rebuild
```

Any model you have already pulled will work; the default `qwen3.5:2b` is the
validated sweet spot for speed vs quality on a typical developer machine.

## Troubleshooting

### SessionStart says setup incomplete

SessionStart probes the **product** daemon at `:11435` and the managed binary
under the plugin data dir — not whether `ollama` is on your shell `PATH`.
System ollama is optional.

If the notice fires once, fix with `/total-recall:llm-setup`, or opt out of
chat refine with `TOTAL_RECALL_LLM_PROVIDER=none`. Dense embeds stay on unless
`TOTAL_RECALL_VEC=0`. Leave `TOTAL_RECALL_EMBED_MODEL` unset (default
`qwen3-embedding:0.6b`).

### Daemon not starting

```bash
# Prefer product path via /total-recall:llm-setup — not system `ollama serve`
curl http://127.0.0.1:11435/api/tags   # product daemon
```

Product defaults to **:11435** so system ollama on :11434 never collides.
Only pin `TOTAL_RECALL_LLM_BASE_URL` if you intentionally own that endpoint
with the **product** binary (foreign daemons are refused).
### Disk space

The model download is ~2.7 GB. Check available space before letting the
bootstrap run on a tight disk:

```bash
df -h "${CLAUDE_PLUGIN_DATA:-$HOME/.local/share}/total-recall/"
```

To abort the download and opt out: set `TOTAL_RECALL_LLM_PROVIDER=none` and
remove the `.llm_provisioning` lockfile from the plugin data dir.

### Manual setup

If auto-provisioning fails for any reason, the `/total-recall:llm-setup` slash
command runs the same steps interactively and reports errors.
