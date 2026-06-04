#!/usr/bin/env python3
"""SessionStart(compact) restore: load the persisted continuation packet and
render it as additionalContext.

Deterministic restore surface for compaction continuity. ``pre-compact-seed.sh``
persists ``sessions/<session_id>.continuation.json``; this helper loads it
(falling back to the newest ``*.continuation.json`` for the same project_key
within 24h), renders it compactly, and clears the ``continuation_pending``
flag in the session state so the UserPromptSubmit bridge doesn't double-surface.

Prints the rendered block to stdout (capped). Empty stdout means "nothing to
restore" -> the bash wrapper emits no envelope. Exit code is always 0.

Mirrors the defensive ``sys.path`` bootstrap of decide_and_format.py / query.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _add_repo_root_to_syspath() -> None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "extractors" / "continuation_packet.py").exists():
            if str(parent) not in sys.path:
                sys.path.append(str(parent))
            return
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and root not in sys.path:
        sys.path.append(root)


_add_repo_root_to_syspath()

# 24h window for the project_key fallback.
_FALLBACK_WINDOW_S = 24 * 3600


def _sessions_dir() -> Path:
    base = Path(
        os.environ.get(
            "CLAUDE_PLUGIN_DATA",
            os.path.expanduser("~/.claude/plugins/data"),
        )
    ).expanduser()
    return base / "total-recall" / "sessions"


def _project_key(cwd: str | None):
    if not cwd:
        return None
    try:
        from extractors.continuation_packet import _project_key as pk  # type: ignore

        return pk(cwd)
    except Exception:
        return cwd


def _load_packet(session_id: str, cwd: str | None) -> dict | None:
    """Direct hit on <session_id>.continuation.json, else newest sibling for
    the same project_key within 24h."""
    sdir = _sessions_dir()
    if session_id:
        direct = sdir / f"{session_id}.continuation.json"
        if direct.exists():
            try:
                return json.loads(direct.read_text())
            except Exception:
                pass

    # Fallback: newest *.continuation.json whose state file shares our
    # project_key, modified within the window.
    want_key = _project_key(cwd)
    if not sdir.exists():
        return None
    now = time.time()
    best = None
    best_mtime = 0.0
    for f in sdir.glob("*.continuation.json"):
        try:
            mtime = f.stat().st_mtime
        except Exception:
            continue
        if now - mtime > _FALLBACK_WINDOW_S:
            continue
        # Match project_key via the sibling state file's cwd when available.
        sib = sdir / (f.name[: -len(".continuation.json")] + ".json")
        sib_key = None
        if sib.exists():
            try:
                sib_state = json.loads(sib.read_text())
                sib_key = _project_key(sib_state.get("cwd"))
            except Exception:
                sib_key = None
        if want_key is not None and sib_key is not None and sib_key != want_key:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            try:
                best = json.loads(f.read_text())
            except Exception:
                best = None
    return best


def _clear_pending(session_id: str) -> None:
    if not session_id:
        return
    try:
        from hooks.lib import session_state as ss  # type: ignore

        state = ss.load_state(session_id)
        if state.get("continuation_pending"):
            state["continuation_pending"] = False
            ss.save_state(state)
    except Exception:
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="compact_restore")
    p.add_argument("--session", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--max-chars", type=int, default=8000)
    args = p.parse_args(argv)

    packet = _load_packet(args.session, args.cwd or None)
    if not isinstance(packet, dict) or set(packet) <= {"_kind"}:
        return 0

    try:
        from extractors.continuation_packet import render_continuation_packet

        body = render_continuation_packet(packet, max_chars=max(200, args.max_chars))
    except Exception:
        return 0

    if not body:
        return 0

    # Restoring is a one-shot: clear the pending flag so the UserPromptSubmit
    # bridge doesn't re-surface the same packet on the next turn.
    _clear_pending(args.session)

    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
