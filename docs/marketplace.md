# total-recall on the 88plug marketplace

## How to install (end users)

```
/plugin marketplace add 88plug/claude-code-plugins
/plugin install total-recall@88plug
```

The 88plug marketplace repo name is **`88plug/claude-code-plugins`**
(canonical name from `~/.claude/plugins/marketplaces/88plug/.claude-plugin/marketplace.json`
→ `"name": "88plug"`; the marketplace itself lives at that local path once cloned
— remote URL is `https://github.com/88plug/claude-code-plugins`).

## What happens on install

1. Claude Code clones `github.com/88plug/total-recall` into
   `~/.claude/plugins/cache/88plug/total-recall/<sha>/`
2. The plugin's hooks register automatically (SessionStart signpost,
   UserPromptSubmit retrieval, PreCompact seed, PostCompact recovery,
   Stop indexer)
3. The `.mcp.json` bundled in the plugin auto-registers the MCP server
4. The 2 skills (recall, speak-like-operator) become available
5. The 15 slash commands become available
6. First Stop hook detects empty DB → detaches
   `total-recall index --full --jobs $(nproc)` → user sees one-shot
   "indexing in background" banner

## Updating the 88plug marketplace.json

Append `marketplace-entry.json`'s contents to the `plugins` array in
`~/.claude/plugins/marketplaces/88plug/.claude-plugin/marketplace.json`,
then commit + push the marketplace repo.

## For multi-CLI users

See `docs/install/` for per-CLI install instructions (OpenCode, Cursor,
Gemini CLI, Codex, Continue, Cline). The 88plug marketplace path is the
Claude-Code-only path — other CLIs install via their own MCP config.

---

## Current plugin metadata (v0.9.0)

`.claude-plugin/plugin.json` ships with `displayName: "Total Recall"`, a description that
reflects the current counts (26 MCP tools, 5 hooks, 15 slash commands, 2 skills + optional
local-LLM refinement layer), and the appropriate keyword set. No outstanding gaps vs.
the marketplace schema.
