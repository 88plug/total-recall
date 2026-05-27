#!/usr/bin/env python3
"""Bridge between the bash ``UserPromptSubmit`` hook and the Python signal layer.

Pipeline (all best-effort, all silent on error):

1. Load per-session state (CW4: ``hooks.lib.session_state``).
2. Detect scope shift (CW2: ``hooks.lib.scope_detect``) using the rolling
   5-prompt window plus the current cwd.
3. Decide whether to re-inject (CW1:
   ``detector.reinjection.should_reinject``) using a ``SessionState`` built
   from the on-disk state.
4. If a shift fired, build a scope-delta payload (CW3:
   ``hooks.lib.scope_delta.compute_scope_delta``); otherwise build a
   per-prompt relevance payload via ``index.query`` (the existing
   ``hooks/lib/query.py`` path).
5. Advance the turn counter and (if we emitted anything) call
   ``record_inject`` so the cache-fresh / session-cap suppression in
   ``should_reinject`` sees the update next turn.

Prints the payload to stdout. Empty stdout means "no envelope" — the bash
wrapper translates that to "skip ``recall::emit_context``".

Exit code is always 0. Any uncaught exception is swallowed and stdout is
left empty so a buggy signal layer can never break the user's turn.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


# --------------------------------------------------------------------------- #
# defensive sys.path bootstrap (same pattern as query.py)
# --------------------------------------------------------------------------- #


def _add_repo_root_to_syspath() -> None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "detector" / "__init__.py").exists() or (
            parent / "hooks" / "lib" / "scope_detect.py"
        ).exists():
            if str(parent) not in sys.path:
                sys.path.append(str(parent))
            return
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and root not in sys.path:
        sys.path.append(root)


_add_repo_root_to_syspath()


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #


def _build_session_state(state: dict, project_known: bool):
    """Adapt our on-disk dict to ``detector.reinjection.SessionState``.

    We don't track escalation in the state file (CW6 owns that), so we
    default ``escalation_risk=0`` and an empty trigger list — meaning the
    only hard signals that can fire from this hook today are
    ``explicit_recall_phrase``, ``post_compact_known_project``, and
    ``scope_shift_to_known_project``.
    """
    from detector.reinjection import SessionState  # type: ignore

    last_inject_ts = float(state.get("last_inject_ts") or 0.0)
    silence = time.time() - last_inject_ts if last_inject_ts else 0.0
    return SessionState(
        cwd=state.get("cwd", "") or "",
        prev_cwd=state.get("prev_cwd"),
        last_inject_ts=last_inject_ts,
        turns_since_inject=int(state.get("turns_since_inject", 0)),
        post_compact=bool(state.get("post_compact", False)),
        project_known=project_known,
        last_assistant_was_tool_heavy=bool(
            state.get("last_assistant_was_tool_heavy", False)
        ),
        silence_seconds=silence,
        same_topic_streak=int(state.get("same_topic_streak", 0)),
        escalation_risk=0,
        escalation_triggers=[],
    )


def _project_known(db_path: Path, cwd: str) -> bool:
    """Cheap "have we indexed this cwd?" check. Treat any failure as False."""
    if not cwd or not db_path.exists():
        return False
    try:
        import sqlite3

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0) as c:
            row = c.execute(
                "SELECT 1 FROM sessions WHERE cwd = ? LIMIT 1", (cwd,)
            ).fetchone()
            return row is not None
    except Exception:
        return False


def _fallback_prompt_relevant(prompt: str, cwd: str) -> str:
    """Reuse the existing ``hooks/lib/query.py`` path to build a generic payload.

    Returns "" if anything goes wrong — the hook will just emit nothing.
    """
    try:
        import io
        import contextlib

        from hooks.lib import query as q  # type: ignore

        ns = argparse.Namespace(
            prompt=prompt, cwd=cwd, limit=3, max_tokens=400
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            q.cmd_prompt_relevant(ns)
        return buf.getvalue().strip()
    except Exception:
        return ""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="decide_and_format")
    p.add_argument("--session", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--db", required=True)
    args = p.parse_args(argv)

    try:
        from hooks.lib import session_state as ss  # type: ignore
        from hooks.lib.scope_detect import detect_scope_shift  # type: ignore
        from detector.reinjection import should_reinject  # type: ignore
    except Exception:
        return 0  # signal layer not importable → no envelope

    db_path = Path(args.db)

    # 1. Load state and compute scope shift before we mutate counters.
    state = ss.load_state(args.session)
    recent_for_shift = list(state.get("recent_prompts", []))  # excludes current
    last_cwd = state.get("cwd") or None
    shift = None
    try:
        shift = detect_scope_shift(
            current_prompt=args.prompt,
            recent_prompts=recent_for_shift,
            current_cwd=args.cwd,
            last_cwd=last_cwd,
        )
    except Exception:
        shift = None

    # 2. Advance the turn counters BEFORE the decision so things like
    #    ``turns_since_inject`` and ``recent_prompts`` reflect this turn.
    try:
        ss.advance_turn(state, args.cwd, args.prompt)
    except Exception:
        pass

    # 3. Ask the signal layer whether to fire.
    project_known = _project_known(db_path, args.cwd)
    fire = False
    try:
        sess = _build_session_state(state, project_known=project_known)
        fire, _reasons = should_reinject(sess, args.prompt)
    except Exception:
        fire = False

    # 4. Build the payload. Scope shifts get the delta block; everything
    #    else falls back to the existing prompt-relevant retrieval.
    payload = ""
    if fire:
        if shift is not None:
            try:
                from hooks.lib.scope_delta import compute_scope_delta  # type: ignore

                payload = compute_scope_delta(
                    db_path=db_path,
                    from_scope=shift.old,
                    to_scope=shift.new,
                ) or ""
                if payload:
                    state["last_scope"] = shift.new
            except Exception:
                payload = ""
        if not payload:
            payload = _fallback_prompt_relevant(args.prompt, args.cwd)

    # 5. Record the injection (if any) and persist state. Always save —
    #    even on a no-fire turn we still want the turn counter and
    #    recent_prompts window updated for next time.
    if payload:
        try:
            ss.record_inject(state)
        except Exception:
            pass
    try:
        ss.save_state(state)
    except Exception:
        pass

    if payload:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
