# Installing total-recall in an agent CLI

total-recall ships an **MCP server** (26 tools — recall, get_operator_context, check_banned, ...) and a **session-ingest pipeline** that reads transcripts from every supported agent CLI. The two surfaces are independent: a CLI that does not support MCP can still have its sessions ingested for backfill context.

| CLI | MCP | Hooks | Session ingest | Doc |
| --- | --- | --- | --- | --- |
| Claude Code | yes | yes | yes | [claude_code.md](./claude_code.md) |
| OpenCode | yes | no | yes | [opencode.md](./opencode.md) |
| Gemini CLI | yes | no | yes | [gemini_cli.md](./gemini_cli.md) |
| Codex CLI | yes | no | yes | [codex.md](./codex.md) |
| Cursor | yes | no | yes | [cursor.md](./cursor.md) |
| Continue | yes | no | yes | [continue.md](./continue.md) |
| Cline | yes | no | yes | [cline.md](./cline.md) |
| Aider | no | no | yes | [aider.md](./aider.md) |
| Goose | yes | no | yes | [goose.md](./goose.md) |
| Grok | yes | no | yes | [grok.md](./grok.md) |

## After installing

Verify the session-ingest side is finding what you expect:

```bash
total-recall sources list      # registry + config + on-disk status
total-recall sources detect    # only sources with detectable data
total-recall sources test <name>
```

Suppress a source you do not want indexed:

```bash
total-recall sources disable <name>
```

The CLI persists choices to `${CLAUDE_PLUGIN_DATA}/total-recall/sources.json`. Default policy is **all sources enabled** — adapters with no on-disk data simply yield zero sessions.
