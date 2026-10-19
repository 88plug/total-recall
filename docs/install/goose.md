# total-recall in Goose

## Status
- MCP support: yes (via Claude Code or any MCP-capable host)
- Hook support: Stop and SessionEnd ingest only
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

## Compaction

Goose's hook spec has no `PreCompact`, `PostCompact`, or compaction event, and stdout is a decision channel rather than an `additionalContext` inject. A block decision is ignored on every event except `PreToolUse` and `Stop`. Shipping a fake `PreCompact` rule would be ignored.

What the docs allow is ingest. Symlink or copy `adapters/goose/` to `~/.agents/plugins/total-recall/` so Goose loads `hooks/hooks.json`. `Stop` and `SessionEnd` run `ingest-on-stop.sh`, which ingests that `session_id` and exits 0 with empty stdout (so `Stop` does not block). The script runs `uv run --project` against the newest marketplace checkout when `python3 -m total_recall` is not installed. `TOTAL_RECALL_PY` overrides that. `TOTAL_RECALL_DB` or `TOTAL_RECALL_DB_DIR` selects the index. There is still no way to inject a recall into the summary or the next turn.

## Caveats
- Archived sessions are skipped by default. The adapter exposes `include_archived=True`
  if you want them included (requires direct adapter instantiation).
- `created_timestamp` is epoch seconds (Goose stores UTC seconds, not milliseconds).
- Sessions with zero messages are skipped.
- Corrupt `content_json` rows are skipped silently — the adapter logs a warning.
