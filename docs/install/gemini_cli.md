# total-recall in Gemini CLI

## Status
- MCP support: yes (stdio transport, via `~/.gemini/settings.json` or `gemini mcp add`)
- Hook support: no
- Session storage: `~/.gemini/tmp/<sessionId>/logs.json` (replay-style)
- Adapter complexity: ~580 LOC (replay reconstruction + tool-call translation)

## Install the MCP server

1. Install total-recall so `python -m total_recall.mcp_server` works:

   ```bash
   pip install total-recall
   ```

2. Either drop this into `~/.gemini/settings.json`:

   ```json
   {"mcpServers":{"total-recall":{"command":"python","args":["-m","total_recall.mcp_server"],"timeout":30000,"trust":false}}}
   ```

   …or use the CLI helper:

   ```bash
   gemini mcp add -s user total-recall python -m total_recall.mcp_server
   ```

3. Restart Gemini CLI (the MCP registry is read at startup).
4. Verify: `gemini mcp list` should show `total-recall`.

## What you get
- 23 MCP tools (recall, get_operator_context, check_banned, ...).
- No hook integration — Gemini CLI has no hook surface yet.

## Session ingest

total-recall autodetects Gemini CLI sessions by walking `~/.gemini/tmp/<sessionId>/logs.json` and replaying the event log into canonical records. Verify:

```bash
total-recall sources test gemini_cli
```

## Caveats
- `trust: false` keeps the server sandboxed — recommended. Set `true` only if you understand the security implications.
- The replay layer reconstructs message/part order from event timestamps; sessions with clock skew may end up with slightly out-of-order assistant turns.
- Function-call and function-response events are translated into `tool_use` / `tool_result` blocks so downstream extractors don't need a special case for Gemini.
