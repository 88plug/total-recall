# total-recall in Cline

## Status
- MCP support: yes (`~/.cline/mcp.json` or the VSCode UI under "Cline: MCP Servers")
- Hook support: no
- Session storage: Cline stores per-task conversations under its VSCode extension state; ingest support is planned (no `lib.sources.cline` adapter ships yet at the time of writing).
- Adapter complexity: pending

## Install the MCP server

1. Install total-recall and `uv`:

   ```bash
   pip install total-recall
   ```

2. Drop this into `~/.cline/mcp.json` (or paste the same JSON into the VSCode "Add MCP Server" dialog):

   ```json
   {"mcpServers":{"total-recall":{"command":"uv","args":["run","--directory","/path/to/total-recall","python","-m","mcp_server"],"disabled":false,"autoApprove":[]}}}
   ```

   Replace `/path/to/total-recall` with the absolute path to your total-recall checkout / installed plugin.

3. Reload the Cline panel in VSCode (or close and reopen the editor).
4. Verify: Cline's MCP Servers panel should list `total-recall` with a green dot.

## What you get
- 26 MCP tools (recall, get_operator_context, check_banned, ...).
- No hook integration — Cline does not expose lifecycle hooks today.

## Session ingest

`total-recall sources test cline` probes the shipped Cline adapter (`lib/sources/cline.py`),
which reads Cline's per-session JSON history. Ingest is automatic — run `total-recall index`
(or let the Stop/PostCompact hooks fire) and Cline sessions are backfilled into the same index
as every other source.

## Caveats
- `disabled: false` and `autoApprove: []` are Cline-specific config flags; the latter must stay empty unless you have a specific tool you want to skip the approval prompt for.
- Cline exposes no lifecycle hooks, so SessionStart/UserPromptSubmit auto-injection does not fire
  inside Cline — recall there is via explicit MCP calls during the live conversation. Cross-session
  backfill still happens via the adapter.
