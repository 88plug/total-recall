# total-recall in Cursor

## Status
- MCP support: yes (stdio, via `~/.cursor/mcp.json` global or `<repo>/.cursor/mcp.json` per-project)
- Hook support: no
- Session storage: `~/.cursor/projects/<projHash>/agent-transcripts/*.jsonl` (v1 / current CLI), `state.vscdb` (v2 / planned)
- Adapter complexity: ~370 LOC (JSONL only at v1)

## Install the MCP server

1. Install total-recall and make sure `python -m mcp_server` resolves (the example uses the in-tree module name; `total-recall mcp` works equivalently if the console script is on `$PATH`).

   ```bash
   pip install total-recall
   ```

2. Drop this into `~/.cursor/mcp.json` for global config, or `<repo>/.cursor/mcp.json` for per-project:

   ```json
   {"mcpServers":{"total-recall":{"command":"python","args":["-m","mcp_server"],"env":{"TOTAL_RECALL_DB_DIR":"${userHome}/.total-recall"}}}}
   ```

3. Restart Cursor (or use the in-app MCP reload).
4. Verify: Cursor's Settings → MCP panel should show `total-recall` and its tools.

## What you get
- 23 MCP tools (recall, get_operator_context, check_banned, ...).
- No hooks — Cursor exposes no lifecycle hook surface.

## Session ingest

total-recall autodetects Cursor agent-transcripts. Verify:

```bash
total-recall sources test cursor
```

## Caveats
- v1 only ingests the newer `agent-transcripts/*.jsonl` tree (~70% of present-day usage). The legacy SQLite (`state.vscdb`) parser is v2 / planned.
- Cursor names project directories by an opaque SHA of the absolute workspace path. The v1 adapter stores the hash itself as the `cwd` surrogate and marks `extra["unresolved_cwd"] = True`. A future helper will let you patch in real cwds.
- `${userHome}` is Cursor's own variable expansion; outside Cursor you must expand it yourself.
