# total-recall in Claude Code

## Status
- MCP support: yes
- Hook support: yes (SessionStart, UserPromptSubmit, Stop, PreCompact, PostCompact)
- Session storage: `~/.claude/projects/<slug>/<session-uuid>.jsonl`
- Adapter complexity: ~100 LOC (thin wrapper over `lib.jsonl_walker`)

## Install (recommended)

Marketplace path — hooks, MCP, skills, and commands register automatically:

```text
/plugin marketplace add 88plug/claude-code-plugins
/plugin install total-recall@88plug
```

Dev checkout:

```bash
git clone https://github.com/88plug/total-recall.git
cd total-recall
uv sync
claude --plugin-dir "$PWD"
```

## Manual MCP server (optional)

The plugin ships `.mcp.json` so you usually do not need this. For a bare MCP host
or custom wiring, run via `uv`:

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

Restart Claude Code (or reload via `/mcp`). Verify: `/mcp` should list
`total-recall` and its 26 tools.

## What you get
- 26 MCP tools (`recall`, `get_operator_context`, `check_banned`, `get_voice_profile`, …).
- Full hook integration via `hooks/hooks.json` (SessionStart signpost + compact-restore,
  UserPromptSubmit retrieval, Stop/PostCompact re-index, PreCompact continuity seed).
- Real-time session ingest: the live `.jsonl` is tailed, so recall reflects the current
  conversation within seconds.

## Session ingest

total-recall autodetects Claude Code sessions under `~/.claude/projects`. Verify:

```bash
total-recall sources test claude_code
```

## Caveats
- The plugin needs `uv` on `$PATH` for the snippet above. Install via the Astral installer if missing.
- `${CLAUDE_PLUGIN_DATA}` is only expanded by Claude Code itself — if you launch the server outside Claude Code, set `TOTAL_RECALL_DB_DIR` explicitly.
- Sidechain (Task subagent) transcripts are stored in the same JSONL and ingested by default; they show up as `is_sidechain=1` rows.
