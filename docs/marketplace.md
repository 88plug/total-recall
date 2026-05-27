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

## To fix before publishing

The following differences exist between amnesia's `plugin.json` (the
reference) and total-recall's `.claude-plugin/plugin.json`:

| Field        | amnesia (reference)           | total-recall (current)        | Action needed |
|--------------|-------------------------------|--------------------------------|---------------|
| `displayName`| `"Amnesia"`                   | **missing**                    | Add `"displayName": "Total Recall"` |
| `description`| Full sentence, no stale counts| References "14 extractors", "22-tool MCP server" — counts may be stale | Update to match actual shipped counts |
| `keywords`   | 7 entries, includes `"session"` | 8 entries, no `"session"` | Minor; add `"session"` if desired |

The missing `displayName` is the only structural gap vs. amnesia's schema.
Stale counts in `description` should be corrected to match what actually
ships in v0.7.2 (23 tools, 5 hooks, 15 slash commands, 2 skills per this spec).
