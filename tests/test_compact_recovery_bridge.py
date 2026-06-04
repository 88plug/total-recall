"""FIX 3 e2e bridge: post-compact-recovery.sh -> session_state -> reinjection.

The PostCompact recovery hook writes a per-session flag file; the
UserPromptSubmit pipeline reads it via ``hooks.lib.session_state.load_state``
and ``detector.reinjection.should_reinject`` turns ``post_compact`` into a
hard re-inject trigger. A prior path mismatch (writer wrote ``session_state/``
while the reader reads ``sessions/``) meant the flag never reached the
consumer. This test exercises the whole chain against the *real* writer path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def plugin_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    return tmp_path


def _run_recovery(cwd: str, session_id: str, env_extra: dict | None = None):
    script = REPO_ROOT / "hooks" / "post-compact-recovery.sh"
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", str(script)],
        input=json.dumps(
            {
                "session_id": session_id,
                "cwd": cwd,
                "hook_event_name": "PostCompact",
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_recovery_flag_reaches_session_state_reader(plugin_data, monkeypatch):
    """recovery.sh -> the path session_state.load_state actually reads."""
    import importlib
    import hooks.lib.session_state as ss
    importlib.reload(ss)

    sid = "bridge-sess-1"
    proc = _run_recovery("/home/operator/proj", sid,
                         env_extra={"CLAUDE_PLUGIN_DATA": str(plugin_data)})
    assert proc.returncode == 0, proc.stderr

    # The writer must have written to the reader's path: sessions/<sid>.json.
    expected = plugin_data / "total-recall" / "sessions" / f"{sid}.json"
    assert expected.exists(), (
        "recovery.sh did not write to the path session_state reads; "
        f"missing {expected}"
    )

    state = ss.load_state(sid)
    assert state["post_compact"] is True
    assert state.get("cwd") == "/home/operator/proj"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_recovery_flag_drives_reinjection(plugin_data, monkeypatch):
    """The post_compact flag set by recovery.sh fires should_reinject."""
    import importlib
    import hooks.lib.session_state as ss
    importlib.reload(ss)
    from detector.reinjection import SessionState, should_reinject

    sid = "bridge-sess-2"
    proc = _run_recovery("/home/operator/proj", sid,
                         env_extra={"CLAUDE_PLUGIN_DATA": str(plugin_data)})
    assert proc.returncode == 0, proc.stderr

    state = ss.load_state(sid)
    assert state["post_compact"] is True

    # Build the reinjection view from the loaded flag. project_known=True
    # because the cwd is indexed; post_compact + known project is a hard
    # re-inject trigger.
    sess = SessionState(
        cwd=state.get("cwd") or "/home/operator/proj",
        prev_cwd=None,
        last_inject_ts=0.0,
        turns_since_inject=0,
        post_compact=bool(state["post_compact"]),
        project_known=True,
        last_assistant_was_tool_heavy=False,
        silence_seconds=0.0,
        same_topic_streak=0,
        escalation_risk=0,
    )
    decision, reasons = should_reinject(sess, last_user="continue please")
    assert decision is True
    assert "hard:post_compact_known_project" in reasons


def test_decide_and_format_surfaces_pending_continuation(plugin_data, monkeypatch):
    """Belt #3: decide_and_format prepends the persisted packet rendering when
    continuation_pending is set, then clears the flag."""
    import importlib
    import io
    import contextlib

    import hooks.lib.session_state as ss
    importlib.reload(ss)
    from hooks.lib import decide_and_format as daf

    sid = "daf-pending-1"
    cwd = "/home/operator/proj"

    # Persist a continuation packet for this session (what pre-compact-seed
    # would have written).
    sessions = plugin_data / "total-recall" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.continuation.json").write_text(
        json.dumps(
            {
                "last_user_directive": "belt3 surfaces this directive",
                "files_in_flight": [{"path": "/home/operator/proj/p.py", "verb": "Edit"}],
                "_kind": "continuation_packet",
            }
        )
    )

    # Seed session state with the pending flag (post-compact-recovery's job).
    state = ss.load_state(sid)
    state["cwd"] = cwd
    state["continuation_pending"] = True
    ss.save_state(state)

    db = plugin_data / "total-recall" / "index.db"  # absent → no fire/fallback
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = daf.main(
            ["--session", sid, "--cwd", cwd, "--prompt", "continue", "--db", str(db)]
        )
    out = buf.getvalue()

    assert rc == 0
    assert out.startswith("[total-recall] POST-COMPACTION CONTINUATION")
    assert "belt3 surfaces this directive" in out

    # Flag consumed.
    after = ss.load_state(sid)
    assert after.get("continuation_pending") is False


def test_decide_and_format_no_pending_no_continuation(plugin_data, monkeypatch):
    """Without continuation_pending the continuation block is never surfaced."""
    import importlib
    import io
    import contextlib

    import hooks.lib.session_state as ss
    importlib.reload(ss)
    from hooks.lib import decide_and_format as daf

    sid = "daf-nopending-1"
    cwd = "/home/operator/proj"

    sessions = plugin_data / "total-recall" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.continuation.json").write_text(
        json.dumps({"last_user_directive": "should NOT surface", "_kind": "continuation_packet"})
    )
    # No continuation_pending flag set.
    ss.save_state({**ss.load_state(sid), "cwd": cwd})

    db = plugin_data / "total-recall" / "index.db"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = daf.main(
            ["--session", sid, "--cwd", cwd, "--prompt", "continue", "--db", str(db)]
        )
    out = buf.getvalue()

    assert rc == 0
    assert "POST-COMPACTION CONTINUATION" not in out
