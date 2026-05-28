# total-recall in Claude Code

## Status
- MCP support: yes
- Hook support: yes (UserPromptSubmit, SessionStart, PreToolUse, Stop, ...)
- Session storage: `~/.claude/projects/<slug>/<session-uuid>.jsonl`
- Adapter complexity: ~100 LOC (thin wrapper over `lib.jsonl_walker`)

## Install the MCP server

total-recall is shipped as a plugin: the MCP server lives inside the
plugin directory and runs via `uv` so no manual venv work is required.

1. Install the plugin (or pin the dev checkout) so `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` resolve in your Claude Code config.
2. Drop this into `~/.claude/claude_desktop_config.json` (or the equivalent project-level config):

   ```json
   {
     "mcpServers": {
       "total-recall": {
         "command": "uv",
         "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "python", "-m", "mcp_server"],
         "env": {"TOTAL_RECALL_DB_DIR": "${CLAUDE_PLUGIN_DATA}/total-recall"}
       }
     }
   }
   ```

3. Restart Claude Code (or reload via `/mcp`).
4. Verify: `/mcp` should list `total-recall` and its 26 tools.

## What you get
- 26 MCP tools (recall, get_operator_context, check_banned, get_voice, get_decisions, ...).
- Full hook integration: hooks under `hooks/` fire on UserPromptSubmit, SessionStart, PreToolUse, Stop, etc. — see `hooks/README.md` in the plugin.
- Real-time session ingest: the live `.jsonl` is tailed, so recall reflects the current conversation within seconds.

## Session ingest

total-recall autodetects Claude Code sessions under `~/.claude/projects`. Verify:

```bash
total-recall sources test claude_code
```

## Caveats
- The plugin needs `uv` on `$PATH` for the snippet above. Install via the Astral installer if missing.
- `${CLAUDE_PLUGIN_DATA}` is only expanded by Claude Code itself — if you launch the server outside Claude Code, set `TOTAL_RECALL_DB_DIR` explicitly.
- Sidechain (Task subagent) transcripts are stored in the same JSONL and ingested by default; they show up as `is_sidechain=1` rows.
