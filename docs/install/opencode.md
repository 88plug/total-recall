# total-recall in OpenCode

## Status
- MCP support: yes (`local` MCP type, stdio transport)
- Hook support: no (OpenCode has no hook system at the time of writing)
- Session storage: `${OPENCODE_DATA_DIR:-~/.local/share/opencode}/opencode.db` (SQLite) or `.../storage/` (legacy JSON)
- Adapter complexity: ~640 LOC (two storage backends to translate)

## Install the MCP server

1. Install total-recall so its console script is on `$PATH`:

   ```bash
   pip install total-recall    # or `uv tool install total-recall`
   ```

   The example below uses `uvx` so OpenCode launches the server in a one-shot ephemeral env — no global install required.

2. Drop this into `~/.config/opencode/opencode.json` (or the per-workspace `opencode.json`):

   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "mcp": {
       "total-recall": {
         "type": "local",
         "command": ["uvx", "total-recall-mcp"],
         "enabled": true,
         "environment": {"TOTAL_RECALL_DB_DIR": "${HOME}/.local/share/total-recall"}
       }
     }
   }
   ```

3. Restart OpenCode (or run the in-app reload).
4. Verify: the MCP panel in OpenCode should list `total-recall` and its tools.

## What you get
- 26 MCP tools (recall, get_operator_context, check_banned, ...).
- No hook integration — OpenCode does not expose lifecycle hooks. Use the MCP tools manually or wire them into instructions.

## Session ingest

total-recall autodetects OpenCode sessions from both backends (SQLite + legacy JSON). Verify:

```bash
total-recall sources test opencode
```

Override the data directory by setting `OPENCODE_DATA_DIR` (comma-separated list of paths supported).

## Caveats
- OpenCode's schema has shifted across versions; the adapter tries `SessionTable.info` first, falls back to a `ProjectTable` join, and finally surfaces `cwd=None` if neither yields one.
- Legacy JSON storage and SQLite storage can coexist in the same data dir — both are ingested with stable ordering by filename / row id.
- The `local` MCP type runs the server as a child process; no port to manage.
