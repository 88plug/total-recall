# total-recall in Codex CLI

## Status
- MCP support: yes (stdio, configured via `~/.codex/config.toml`)
- Hook support: yes, via the plugin `hooks/hooks.json` (trust it with `/hooks`)
- Session storage: `${CODEX_HOME:-~/.codex}/sessions/<rollout>.jsonl`
- Adapter complexity: ~570 LOC (replay state machine + token-block normalisation)

## Install the MCP server

1. Install total-recall so the `total-recall` console script is on `$PATH`:

   ```bash
   pip install total-recall
   ```

2. Drop this into `~/.codex/config.toml`:

   ```toml
   [mcp_servers.total_recall]
   command = "total-recall"
   args = ["mcp"]
   startup_timeout_sec = 10
   ```

3. Restart Codex CLI.
4. Verify: launch Codex and ask for "the tools you can see" — it should mention `total-recall`'s tools (or check Codex's own `/mcp` equivalent if your build has one).

## What you get
- 26 MCP tools (recall, get_operator_context, check_banned, ...).
- Compaction: `PreCompact` ingests the rollout file before the summary. Codex does not accept `additionalContext` on `PreCompact`, and `continue: false` aborts compaction, so the hook does not emit either. After compaction, `SessionStart` with `source=compact` injects operator context plus a recall (`total-recall compact-recall`) before the next model request, including a mid-turn auto-compact. The next `UserPromptSubmit` reinject stays as a backup.

## Hooks

Codex loads `.codex-plugin/plugin.json`, which points at `hooks/hooks.json`. Codex sets `PLUGIN_ROOT` and the `CLAUDE_PLUGIN_ROOT` alias, so the same scripts run. New or changed hooks stay skipped until you trust them with `/hooks` (or pass `--dangerously-bypass-hook-trust` for a vetted one-off).

## Session ingest

total-recall autodetects Codex sessions under `~/.codex/sessions`. Verify:

```bash
total-recall sources test codex
```

Override the root with `CODEX_HOME=/some/other/path`.

## Caveats
- TOML keys use underscores, so the section name is `mcp_servers.total_recall` (not `total-recall`). Codex's TOML parser rejects hyphens here.
- Codex stores per-rollout transcripts; sessions appear as separate files but share state via parent-uuid links — the adapter reconstructs them.
- Token-usage payloads have a different shape than Anthropic's; the adapter preserves the original under `usage["_codex_raw"]` so nothing is lost.
