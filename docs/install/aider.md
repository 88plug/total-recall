# total-recall in Aider

## Status
- MCP support: no (Aider has no MCP client today)
- Hook support: no
- Session storage: `.aider.chat.history.md` and `.aider.input.history` at each git-repo root
- Adapter complexity: ~280 LOC

## Install the MCP server

Aider has no MCP integration. There are no CLI-side changes to make.

## What you get
- Session ingest only: total-recall reads `.aider.chat.history.md` files from your git repos to backfill prior context.
- No live MCP tools inside Aider itself.

If you want the 26 MCP tools available *while* coding, run a second agent (Claude Code, OpenCode, Cursor, etc.) alongside Aider and install total-recall there — it will be reading from the same shared SQLite index and will surface Aider's recent chats in `recall` results.

## Session ingest

total-recall walks your git repos looking for `.aider.chat.history.md`. Verify:

```bash
total-recall sources test aider
```

## Caveats
- Aider's history is markdown, not JSONL — the adapter parses the canonical `#### user / #### aider` heading format. Custom Aider themes that rewrite headings will not parse.
- There is no message UUID in Aider's format; the adapter synthesises one from `(file path, sha256(message body))` so re-ingestion is idempotent.
- `cwd` is the repo root containing the history file. If you move a repo, prior Aider sessions still resolve to the old absolute path (they're a snapshot of where the work happened).
