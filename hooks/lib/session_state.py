"""Per-session state file for the signal-driven re-injection pipeline.

The ``UserPromptSubmit`` hook needs to remember a handful of per-session
facts across invocations:

* turn counter,
* most recent prompts (for ``hooks.lib.scope_detect.dominant_scope``),
* timestamp + counter for the last memory injection (so
  :func:`detector.reinjection.should_reinject` can apply the 5-min
  prompt-cache suppression window and the per-session injection cap),
* the prior cwd (so a cwd switch can be recognised),
* whether a ``PostCompact`` hook has fired and not yet been consumed.

The state lives in a single JSON file per session under::

    ${CLAUDE_PLUGIN_DATA}/total-recall/sessions/{session_id}.json

Writes go through a ``.tmp`` rename so a crashed hook can never leave a
half-written file. All read failures degrade silently to a default state
— the hook treats "no prior state" the same as a brand-new session.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


def state_path(session_id: str) -> Path:
    """Resolve the on-disk path for ``session_id``'s state file.

    Mirrors the layout used by ``hooks/lib/common.sh`` so other hooks (in
    bash) can find the same files without reimplementing the lookup.
    Creates the parent dir 0o700 on demand.
    """
    base = Path(
        os.environ.get(
            "CLAUDE_PLUGIN_DATA",
            os.path.expanduser("~/.local/share/total-recall"),
        )
    ).expanduser()
    p = base / "total-recall" / "sessions"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p / f"{session_id}.json"


def load_state(session_id: str) -> dict:
    """Return the state dict for ``session_id``.

    Missing file or any decode error → default state. We never raise — the
    hook treats "no state" the same as "fresh session", which is the safe
    fallback.
    """
    p = state_path(session_id)
    if not p.exists():
        return _default_state(session_id)
    try:
        return json.loads(p.read_text())
    except Exception:
        return _default_state(session_id)


def save_state(state: dict) -> None:
    """Atomically persist ``state`` to its session file.

    Writes to ``{path}.tmp`` then ``os.replace``s into place so a hook
    timeout or crash can never produce a half-written JSON blob.
    ``default=str`` keeps ``datetime``-ish values serialisable without
    forcing every caller to convert by hand.
    """
    p = state_path(state["session_id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, default=str))
    tmp.replace(p)


def _default_state(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "cwd": "",
        "prev_cwd": None,
        "turn": 0,
        "last_inject_ts": 0.0,
        "turns_since_inject": 0,
        "injections_count": 0,
        "post_compact": False,
        "recent_prompts": [],
        "last_assistant_was_tool_heavy": False,
        "same_topic_streak": 0,
        "last_scope": None,
    }


def record_inject(state: dict) -> None:
    """Mark that we just emitted an injection envelope.

    Caller is expected to ``save_state`` afterwards. ``post_compact`` is
    cleared here because PostCompact is a one-shot signal — once we've
    re-injected after a compaction, the trigger is spent.
    """
    state["last_inject_ts"] = time.time()
    state["turns_since_inject"] = 0
    state["injections_count"] = state.get("injections_count", 0) + 1
    state["post_compact"] = False  # consumed


def advance_turn(state: dict, cwd: str, prompt: str) -> None:
    """Roll forward turn counters and the recent-prompt window.

    Bumps ``turn``, slides the prior ``cwd`` into ``prev_cwd``, appends
    ``prompt`` to the last-5 sliding window used by
    :func:`hooks.lib.scope_detect.dominant_scope`. Increments
    ``turns_since_inject`` so the suppression window in
    :func:`detector.reinjection.should_reinject` can track staleness.
    """
    state["prev_cwd"] = state.get("cwd")
    state["cwd"] = cwd
    state["turn"] = state.get("turn", 0) + 1
    state["turns_since_inject"] = state.get("turns_since_inject", 0) + 1
    prompts = state.get("recent_prompts", [])
    prompts.append(prompt)
    state["recent_prompts"] = prompts[-5:]
