"""total-recall SQLite index.

A boring, fast, local index. SQLite + FTS5 for keyword recall, structured
columns for the cheap filters (`cwd`, `session_id`, `kind`, `ts`). Optional
``sqlite-vec`` augmentation lives in a sibling package (`vec/`) and is not a
dependency of this layer.

The index is **append-friendly**: ingest is incremental by byte offset, so a
re-run after a session has appended N new lines reads only those N lines.
Schema lives in :mod:`index.schema` (via ``schema.sql``), the connection
helpers in :mod:`index.db`, and the public read/write API in
:mod:`index.query` / :mod:`index.ingest` respectively.

Public surface:

- :func:`index.db.connect`             — open / create / migrate the DB.
- :func:`index.db.apply_schema`        — idempotent ``schema.sql`` apply.
- :func:`index.ingest.ingest_file`     — incremental single-file ingest.
- :func:`index.ingest.ingest_all`      — walk ``~/.claude/projects`` and ingest.
- :func:`index.query.search_extractions` — typed query API for the MCP layer.
- :func:`index.query.search_messages`    — raw-message FTS5 fallback.
- :func:`index.tail.tail_loop`         — polling tailer (30s default).
"""

from index.db import DEFAULT_DB_PATH, apply_schema, connect, vacuum
from index.ingest import IngestReport, ingest_all, ingest_file
from index.query import (
    QueryHit,
    list_sessions_for_cwd,
    search_extractions,
    search_messages,
    session_count_for_cwd,
    top_topics_for_cwd,
)
from index.tail import tail_loop

__all__ = [
    # db
    "DEFAULT_DB_PATH",
    "connect",
    "apply_schema",
    "vacuum",
    # ingest
    "IngestReport",
    "ingest_file",
    "ingest_all",
    # query
    "QueryHit",
    "search_extractions",
    "search_messages",
    "top_topics_for_cwd",
    "session_count_for_cwd",
    "list_sessions_for_cwd",
    # tail
    "tail_loop",
]
