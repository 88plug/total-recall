"""total-recall parsing library.

Streaming, byte-offset-aware parser for Claude Code session transcripts at
``~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`` plus their sibling
``<session-uuid>/subagents/agent-*.jsonl`` sidechain files.

Public surface:

- :mod:`lib.schema`      — typed dataclasses for every record type.
- :mod:`lib.jsonl_walker` — line-by-line iterator with byte-offset tracking.
- :mod:`lib.dag`         — parent-uuid DAG, leaf detection, linearization.
- :mod:`lib.sidechain`   — subagent transcript loader and parent-linker.

The library is read-only against the on-disk corpus and is intentionally
tolerant: unknown record types, additive new fields, blank lines, and a
truncated final line are all handled without raising.
"""

from lib.dag import Dag, build_dag, find_branches, linearize_main_branch
from lib.jsonl_walker import (
    all_sessions,
    derive_cwd_from_slug,
    derive_slug_from_cwd,
    iter_records,
    session_files_for_cwd,
)
from lib.schema import (
    AgentNameRecord,
    AiTitleRecord,
    AssistantRecord,
    AttachmentRecord,
    Block,
    FileHistorySnapshotRecord,
    LastPromptRecord,
    PermissionModeRecord,
    QueueOperationRecord,
    Record,
    SystemRecord,
    ToolResult,
    ToolUseRef,
    UserRecord,
    parse_record,
)
from lib.sidechain import (
    link_subagents_to_parent,
    load_subagent,
    subagent_files_for_session,
)

__all__ = [
    # schema
    "Record",
    "AssistantRecord",
    "UserRecord",
    "SystemRecord",
    "AttachmentRecord",
    "QueueOperationRecord",
    "AiTitleRecord",
    "LastPromptRecord",
    "PermissionModeRecord",
    "FileHistorySnapshotRecord",
    "AgentNameRecord",
    "Block",
    "ToolUseRef",
    "ToolResult",
    "parse_record",
    # walker
    "iter_records",
    "session_files_for_cwd",
    "all_sessions",
    "derive_cwd_from_slug",
    "derive_slug_from_cwd",
    # dag
    "Dag",
    "build_dag",
    "linearize_main_branch",
    "find_branches",
    # sidechain
    "subagent_files_for_session",
    "load_subagent",
    "link_subagents_to_parent",
]
