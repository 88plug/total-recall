# Registration Note — model_corrections (I2/10)

This worktree adds a new extractor + new MCP tools. The orchestrator wires
them in **post-merge** with two tiny edits.

## 1. Register the extractor in the pipeline

In `extractors/pipeline.py`, add the import and append the instance to
`ALL_EXTRACTORS`.

```python
# top imports
from extractors.model_corrections import ModelCorrections

ALL_EXTRACTORS: list[Extractor] = [
    Corrections(),
    Decisions(),
    SelfCorrections(),
    Progress(),
    DomainFacts(),
    AwaySummaries(),
    ModelCorrections(),   # <-- add
]
```

Order is functional only (no dependency on placement) — keep it last so the
ordering of existing extractors is preserved.

## 2. Wire the MCP tool surface

In `mcp_server/server.py`, add a side-effect import alongside the existing
`from mcp_server import resources as _resources / tools as _tools` block:

```python
from mcp_server import resources as _resources  # noqa: E402,F401
from mcp_server import tools as _tools          # noqa: E402,F401
from mcp_server.extras import corrections_tools as _corrections_tools  # noqa: E402,F401
```

This registers the two new `@mcp.tool()` handlers:

- `recall_corrections_about(topic, scope, limit)` — HIGH-PRIORITY: call
  before suggesting defaults that may have been previously rejected.
- `get_recent_corrections(cwd, since_days, limit)` — useful at session
  start to see what the model has been doing wrong lately.

## 3. (Optional) Extend the `kind` enum on `recall()`

`mcp_server/tools.py::recall()` carries a `Literal[...]` enum for the
`kind` filter. If the team wants `recall(kind="model_correction")` to also
work, add `"model_correction"` to that Literal. **Not strictly required**
— the two new tools already cover the surface and `recall()` with
`kind="any"` will still return model corrections.

## 4. New tests

`tests/test_model_corrections.py` is self-contained and uses the same
fakes as `tests/test_extractors.py` + `tests/test_mcp_tools.py`. It does
not depend on any unmerged worktree.

## Files added

- `extractors/model_corrections.py`
- `mcp_server/extras/__init__.py`
- `mcp_server/extras/corrections_tools.py`
- `tests/test_model_corrections.py`
- `extractors/_REGISTRATION_NOTE.md` (this file)

## Files touched

None — every change above is in newly-created files.
