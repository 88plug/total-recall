# I3/10 — goals extractor + goal stack wiring

This slice adds a new extractor (`Goals` in `extractors/goals.py`), a
goal-stack index module (`index/goals.py`), MCP tools
(`mcp_server/extras/goals_tools.py`), and tests (`tests/test_goals.py`).
The pieces below are what a wiring PR needs to touch outside this slice's
owned files.

## 1. `extractors/pipeline.py` — register `Goals` in `ALL_EXTRACTORS`

```python
from extractors.goals import Goals   # NEW

ALL_EXTRACTORS: list[Extractor] = [
    Corrections(),
    Decisions(),
    SelfCorrections(),
    Progress(),
    DomainFacts(),
    AwaySummaries(),
    Goals(),                          # NEW
]
```

`Goals` emits two kinds: `goal` and `goal_progress`. Both flow through the
same secret-scrubbing path as every other extractor — no special handling
in `run_all`.

## 2. `index/schema.sql` — apply the `goal_stack` schema

Append the `goal_stack` DDL to `index/schema.sql` so it's part of the
single source of truth that `index.db.apply_schema` already runs:

```sql
CREATE TABLE IF NOT EXISTS goal_stack (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  goal_text TEXT NOT NULL,
  declared_ts INTEGER NOT NULL,
  last_progress_ts INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  related_projects TEXT,
  source_session TEXT,
  UNIQUE(project, goal_text)
);
CREATE INDEX IF NOT EXISTS idx_goals_project_status ON goal_stack(project, status);
CREATE INDEX IF NOT EXISTS idx_goals_last_progress ON goal_stack(last_progress_ts DESC);
```

Bump `_CURRENT_SCHEMA_VERSION` in `index/db.py` from `"2"` to `"3"` and add
a v2 → v3 branch to `apply_schema`. The `CREATE … IF NOT EXISTS` makes the
upgrade a free no-op for existing v2 DBs.

Alternatively (and equivalently), call `index.goals.apply_schema(conn)`
from `index.db.apply_schema` after the script runs. The DDL block lives in
`index/goals.GOAL_STACK_SCHEMA` already, so the import-and-call path is
trivial.

## 3. `index/ingest.py` — fold `goal*` extractions into `goal_stack`

Plumbing change in `_parse_file_pure`: keep the raw `Extraction` objects
alongside the flattened tuples (the pipeline already gives us both — just
append them to a new `_ParsedFile.raw_extractions: list[Extraction]`).
Then in `_commit_parsed`, after the `extractions` `executemany` block:

```python
try:
    from index.goals import upsert_from_extractions, recompute_statuses
    upsert_from_extractions(conn, parsed.raw_extractions)
    recompute_statuses(conn)
except sqlite3.OperationalError:
    _warn_metrics_unavailable_once()
except ImportError:
    pass  # bare branch — goals module absent.
```

`recompute_statuses` is cheap (4 indexed UPDATEs) and idempotent, so
running it per-file is fine. If it shows up in profiling, hoist to
end-of-`ingest_all`.

## 4. `mcp_server/server.py` — register the goals tools at startup

Add one side-effect import at the bottom of `mcp_server/server.py`, next
to the existing `from mcp_server import tools as _tools`:

```python
from mcp_server.extras import goals_tools as _goals_tools  # noqa: E402,F401
```

Tools register on the shared `mcp` instance at import time — same pattern
as `resources.py`, `tools.py`, and `extras/corrections_tools.py` (I2/10).

## 5. SessionStart hook surface (future, not in this slice)

A SessionStart hook can call `get_active_goal(cwd)` and, when the result
is a dict (not None / not an error), prepend something like:

```
Last in-flight: {goal_text}. Continue?
```

to the model's system prompt. The MCP tool already returns the right
shape (`{project, goal_text, declared_ts, last_progress_ts, status, ...}`
or `None`); no extra plumbing required beyond reading `$PWD`.

## Owned files (this slice — do not modify outside it)

- `extractors/goals.py`
- `index/goals.py`
- `mcp_server/extras/goals_tools.py`
- `tests/test_goals.py`
- `_REGISTRATION_NOTE_goals.md` (this file)
