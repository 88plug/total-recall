# total-recall in Pi

## Status
- MCP support: register the server from Pi the same way as any stdio MCP server
- Hook support: TypeScript extension, not `hooks.json`
- Session storage: `~/.pi/agent/sessions/**/*.jsonl` (`PI_CODING_AGENT_SESSION_DIR` or `PI_CODING_AGENT_DIR` override this)
- Adapter: `lib/sources/pi.py`

Pi sessions are append-only. A compaction entry is appended and the raw messages stay in the file. The adapter indexes `type:"message"` user and assistant turns and skips compaction entries so the summary is not stored twice.

## Compaction extension

`adapters/pi/total-recall-compact.ts`

```bash
pi --extension /path/to/total-recall/adapters/pi/total-recall-compact.ts
```

Or copy the file to `~/.pi/agent/extensions/`.

`session_before_compact` ingests `ctx.sessionManager.getSessionFile()` with `total-recall index --file … --source pi`. The handler returns undefined. It does not cancel compaction and it does not replace Pi's summarizer (the first custom summary would).

`session_compact` runs only after compaction succeeds. The next `before_agent_start` injects `total-recall compact-recall` as a custom message. That is the post-compact channel Pi documents. `session_compact_failed` clears the pending flag so a failed compaction does not inject on a later turn.

The extension runs `uv run --project` against the newest marketplace checkout when `python3 -m total_recall` is not installed. `TOTAL_RECALL_PY` overrides that. `TOTAL_RECALL_DB` or `TOTAL_RECALL_DB_DIR` selects the index. With neither set, the CLI discovers the existing plugin index.
