"""Subagent (sidechain) transcript loader.

When the parent assistant calls ``TaskCreate``, the harness spawns a
subagent with its own transcript at:

    ``<session-uuid>/subagents/agent-<agentId>.jsonl``

…paired with a ``agent-<agentId>.meta.json`` file containing
``{agentType, description, toolUseId}``.

Subagent records:

* share the *parent's* ``sessionId`` (and ``cwd``);
* carry ``isSidechain: true``;
* form their own DAG — the subagent's first record has ``parentUuid: null``.

The parent's linkage to a subagent is via a ``user`` record whose
``toolUseResult.agentId`` matches the agent's filename slug. That same
record's ``tool_results[0].tool_use_id`` matches the parent
``assistant.tool_use{name:"TaskCreate"}.id`` (and the meta JSON's
``toolUseId``). We surface both connections so callers can pick whichever
fits their downstream model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from lib.jsonl_walker import iter_records
from lib.schema import Record, UserRecord

_AGENT_FILE_RE = re.compile(r"^agent-([A-Za-z0-9]+)\.jsonl$")


def _meta_path_for(agent_jsonl: Path) -> Path:
    return agent_jsonl.with_name(agent_jsonl.stem + ".meta.json")


def _read_meta(agent_jsonl: Path) -> dict:
    mp = _meta_path_for(agent_jsonl)
    if not mp.is_file():
        return {}
    try:
        with mp.open("r", encoding="utf-8", errors="replace") as f:
            obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _subagent_dir_for(session_jsonl: Path) -> Path:
    """``<session-uuid>.jsonl`` → ``<session-uuid>/subagents/``."""
    return session_jsonl.with_suffix("") / "subagents"


def subagent_files_for_session(jsonl_path: Path) -> list[tuple[str, Path]]:
    """Return ``[(agent_id, agent_jsonl_path), ...]`` for one session.

    ``agent_id`` is extracted from the filename
    (``agent-<agent_id>.jsonl``), which matches the parent record's
    ``toolUseResult.agentId``. Empty list if the sidechain dir is missing or
    no agent files exist. Order is sorted by filename for testability.
    """

    sub_dir = _subagent_dir_for(Path(jsonl_path))
    if not sub_dir.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for p in sorted(sub_dir.iterdir()):
        if not p.is_file():
            continue
        m = _AGENT_FILE_RE.match(p.name)
        if not m:
            continue
        out.append((m.group(1), p))
    return out


def load_subagent(agent_jsonl: Path) -> list[Record]:
    """Stream-parse one subagent transcript into a record list.

    The meta-file (``.meta.json`` sibling) is *not* attached here — callers
    that want it should call :func:`load_subagent_meta`. We keep the two
    separate so callers operating in streaming contexts don't pay for IO
    they don't need.
    """

    return [r for _, r in iter_records(Path(agent_jsonl))]


def load_subagent_meta(agent_jsonl: Path) -> dict:
    """Return the ``.meta.json`` sibling as a dict (``{}`` if missing)."""
    return _read_meta(Path(agent_jsonl))


def _parent_tool_use_id_for_agent(
    parent_records: list[Record], agent_id: str
) -> Optional[str]:
    """Find the parent ``user`` tool_result whose ``toolUseResult.agentId``
    matches, and return its inner ``tool_use_id`` so callers can map back to
    the originating assistant ``tool_use`` block.
    """

    for r in parent_records:
        if not isinstance(r, UserRecord):
            continue
        payload = r.tool_use_result_payload or {}
        if payload.get("agentId") != agent_id:
            continue
        if r.tool_results:
            return r.tool_results[0].tool_use_id
        return None
    return None


def link_subagents_to_parent(
    parent_records: list[Record], subagent_dir: Path
) -> dict[str, list[Record]]:
    """Load every subagent for a session and key them by parent ``tool_use_id``.

    The returned mapping uses the parent ``assistant.tool_use{TaskCreate}.id``
    as its key — that's the natural join key for "what did this TaskCreate
    actually do?" If a subagent file exists but no parent tool_use matches
    (or the parent never recorded a completion ``user`` record yet — common
    while the subagent is still running), it is keyed under
    ``"agent:<agent_id>"`` so the records are not lost.

    ``subagent_dir`` is the ``<session-uuid>/subagents/`` directory. Pass
    the result of :func:`_subagent_dir_for` or compute it yourself; we
    accept it explicitly so callers can also pass an alternative location
    in tests.
    """

    out: dict[str, list[Record]] = {}
    sub_dir = Path(subagent_dir)
    if not sub_dir.is_dir():
        return out

    for entry in sorted(sub_dir.iterdir()):
        if not entry.is_file():
            continue
        m = _AGENT_FILE_RE.match(entry.name)
        if not m:
            continue
        agent_id = m.group(1)
        records = load_subagent(entry)

        # Try parent linkage via the user tool_result envelope first.
        key = _parent_tool_use_id_for_agent(parent_records, agent_id)
        if key is None:
            # Fall back to the meta file's toolUseId (works even if the
            # parent's completion record hasn't been written yet).
            meta = _read_meta(entry)
            key = meta.get("toolUseId") or f"agent:{agent_id}"

        out.setdefault(key, []).extend(records)

    return out
