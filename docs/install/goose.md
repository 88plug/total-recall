# total-recall in Goose

## Status
- MCP support: yes (via Claude Code or any MCP-capable host)
- Hook support: no
- Session storage: `~/.local/share/goose/sessions/sessions.db` (SQLite)
- Adapter complexity: ~220 LOC

## Install the MCP server

Goose itself does not load total-recall's MCP server directly. Run total-recall inside
Claude Code (or another MCP host) alongside Goose — it reads from the same shared SQLite
index and surfaces Goose sessions in `recall` results.

## Session ingest

total-recall reads from Goose's SQLite sessions database automatically. Verify:

```bash
total-recall sources test goose
```

The adapter discovers sessions from `~/.local/share/goose/sessions/sessions.db` by
default. Override with `GOOSE_DATA_DIR=/path/to/goose/data`.

## What you get
- Full session ingest: assistant turns, user messages, tool calls and responses.
- Goose sessions appear in `recall`, `search_messages`, and `prior_sessions_for_cwd`
  alongside Claude Code / OpenCode / Codex sessions.
- Dedup: if the same content appears in multiple sources, Claude Code wins by priority.

## Caveats
- Archived sessions are skipped by default. The adapter exposes `include_archived=True`
  if you want them included (requires direct adapter instantiation).
- `created_timestamp` is epoch seconds (Goose stores UTC seconds, not milliseconds).
- Sessions with zero messages are skipped.
- Corrupt `content_json` rows are skipped silently — the adapter logs a warning.
