# total-recall in Grok

## Status
- MCP support: yes (via Claude Code or any MCP-capable host)
- Hook support: no
- Session storage: `~/.grok/sessions/<url-encoded-cwd>/<uuid>/chat_history.jsonl`
- Adapter complexity: ~180 LOC

## Install the MCP server

Grok does not load total-recall's MCP server directly. Run total-recall inside
Claude Code (or another MCP host) — it reads from the same shared SQLite index
and surfaces Grok sessions in `recall` results.

## Session ingest

total-recall reads from Grok's session JSONL files automatically. Verify:

```bash
total-recall sources test grok
```

The adapter discovers sessions under `~/.grok/sessions/` by default. Override with
`GROK_HOME=/path/to/.grok`.

## What you get
- Full session ingest: user messages, assistant turns, tool calls (with double-JSON-decoded
  arguments, same encoding as Codex).
- Grok sessions appear in `recall`, `search_messages`, and `prior_sessions_for_cwd`
  alongside Claude Code and other sources.
- The `cwd` is URL-decoded from the directory name (`%2Fhome%2Fuser%2Fproject` →
  `/home/user/project`) so recall scoping works correctly.

## Caveats
- Session directories are named by URL-encoded CWD. If the CWD contains characters
  that `urllib.parse.unquote` cannot decode, the adapter skips that directory and logs.
- `chat_history.jsonl` uses the same JSONL record format as Codex; tool call arguments
  are double-JSON-encoded (the outer JSON value is itself a JSON string).
