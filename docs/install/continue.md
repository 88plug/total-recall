# total-recall in Continue

## Status
- MCP support: yes (YAML config, `~/.continue/config.yaml`)
- Hook support: no
- Session storage: `~/.continue/sessions/*.json` (JSON-per-session)
- Adapter complexity: ~430 LOC

## Install the MCP server

1. Install total-recall and `uv` (the example below uses `uv run` to launch the server straight from a checkout):

   ```bash
   pip install total-recall    # or skip if running purely via uv
   ```

2. Drop this into `~/.continue/config.yaml`:

   ```yaml
   mcpServers:
     - name: total-recall
       command: uv
       args: [run, --directory, /path/to/total-recall, python, -m, mcp_server]
   ```

   Replace `/path/to/total-recall` with the absolute path to your total-recall checkout (or to the installed plugin directory).

3. Restart Continue, or reload via its settings UI.
4. Verify: Continue's tool list should include the total-recall tools.

## What you get
- 23 MCP tools (recall, get_operator_context, check_banned, ...).
- No hook integration — Continue exposes no lifecycle hooks.

## Session ingest

total-recall autodetects Continue sessions under `~/.continue/sessions`. Verify:

```bash
total-recall sources test continue
```

## Caveats
- YAML is indentation-sensitive; mismatched indent silently disables the server. If `total-recall` doesn't appear after a restart, double-check with `yamllint` or `python -c 'import yaml,sys;yaml.safe_load(open(sys.argv[1]))' ~/.continue/config.yaml`.
- Continue's tool-call schema is closer to OpenAI's than Anthropic's; the adapter translates `function_call` / `function_response` into `tool_use` / `tool_result` blocks.
- The source registers under the name `continue` (without the `_dev` suffix used by the Python module file).
