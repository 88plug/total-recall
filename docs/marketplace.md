# total-recall on the 88plug marketplace

## How to install (end users)

```text
/plugin marketplace add 88plug/claude-code-plugins
/plugin install total-recall@88plug
```

The 88plug marketplace repo is **`88plug/claude-code-plugins`**
(canonical name from the marketplace manifest → `"name": "88plug"`;
remote URL `https://github.com/88plug/claude-code-plugins`).

## What happens on install

1. Claude Code clones `github.com/88plug/total-recall` into
   `~/.claude/plugins/cache/88plug/total-recall/<sha>/`
2. Hooks register automatically (SessionStart signpost + compact-restore,
   UserPromptSubmit retrieval, PreCompact seed, PostCompact recovery + index,
   Stop indexer)
3. The bundled `.mcp.json` auto-registers the MCP server
4. Three skills become available: `recall`, `speak-like-operator`, `llm-setup`
5. Fifteen slash commands become available
6. First Stop hook detects empty DB → detaches
   `total-recall index --full --jobs $(nproc)` → one-shot
   "indexing in background" banner

## Updating the 88plug marketplace.json

Append `marketplace-entry.json`'s contents to the `plugins` array in the
marketplace repo's `.claude-plugin/marketplace.json`, then commit and push that
repo.

## For multi-CLI users

See [Install overview](install/README.md) for per-CLI instructions (OpenCode,
Cursor, Gemini CLI, Codex, Continue, Cline, Aider, Goose, Grok). The 88plug
marketplace path is Claude-Code-only — other CLIs install via their own MCP
config.

---

## Current plugin metadata (v2.3.0)

`.claude-plugin/plugin.json` ships with `displayName: "Total Recall"` and counts
that match the product:

| Surface | Count |
| --- | --- |
| MCP tools | 26 |
| Hooks | 6 (SessionStart ×2, UserPromptSubmit, Stop, PreCompact, PostCompact) |
| Slash commands | 15 |
| Skills | 3 (`recall`, `speak-like-operator`, `llm-setup`) |
| Session sources | 10 CLI clients |

Optional local-LLM refinement (ollama, auto-provisioned; disable with
`TOTAL_RECALL_LLM_PROVIDER=none`). See [Local-LLM refinement](llm-refinement.md).
