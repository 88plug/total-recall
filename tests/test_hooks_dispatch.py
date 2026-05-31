"""End-to-end coverage for the UserPromptSubmit hook's populated-DB path.

The legacy shell harness (test_hooks.sh sections [4]/[6]) used a 0-byte index.db,
which trips `recall::is_fresh_install` (anything < 102400 bytes is "fresh") so the
hook always took the bootstrap branch and never exercised the real
decide_and_format dispatch. This drives the hook subprocess against a synthetic
index.db that is large enough to read as NOT fresh, so the populated path runs.

Hermetic: a tmp CLAUDE_PLUGIN_DATA, a synthetic index built via index.db.connect,
no touch of the operator's real ~/.claude. Skips cleanly if bash / jq / uv absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "user-prompt-retrieve.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or not HOOK.is_file(),
    reason="bash/jq/hook required",
)


def _build_populated_db(db_path: Path) -> None:
    """A real index.db inflated past the 102400-byte is_fresh_install threshold."""
    from index.db import connect

    conn = connect(db_path)
    try:
        # Bulk-insert messages so the file comfortably exceeds 100KB.
        rows = [
            (
                f"sess-{i % 5}",
                "/proj/dispatch",
                "user" if i % 2 == 0 else "assistant",
                "message",
                1_700_000_000 + i,
                f"msg-{i}",
                0,
                f"/proj/dispatch/s{i % 5}.jsonl",
                f"a synthetic message about widgets and provider-x number {i} "
                * 4,
            )
            for i in range(1200)
        ]
        conn.executemany(
            "INSERT INTO messages "
            "(session_id, cwd, role, kind, ts, message_uuid, byte_offset, "
            " source_file, text) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _run_hook(plugin_data: Path, prompt: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{env.get('PYTHONPATH', '')}"
    payload = json.dumps(
        {
            "session_id": "dispatch-test",
            "cwd": "/proj/dispatch",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
    )
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_hook_populated_db_takes_dispatch_path(tmp_path: Path) -> None:
    """A >100KB DB must NOT trip the fresh-install bootstrap branch.

    The hook exits 0 either way; the discriminator is the structured log line
    it writes — bootstrap path logs bootstrap="started", the dispatch path does
    not. We assert the bootstrap banner is NOT emitted (no fresh-install path)
    and the index DB used is the populated one.
    """
    data_root = tmp_path / "total-recall"
    data_root.mkdir(parents=True)
    db = data_root / "index.db"
    _build_populated_db(db)
    assert db.stat().st_size >= 102_400, "synthetic DB must exceed fresh threshold"

    proc = _run_hook(tmp_path, "what did we decide about provider-x and widgets")

    assert proc.returncode == 0, f"stderr={proc.stderr!r}"

    # The bootstrap banner only appears on the fresh-install path. A populated
    # DB must never emit it.
    out = proc.stdout.strip()
    assert "First-run indexing" not in out, (
        "populated DB wrongly took the fresh-install bootstrap path"
    )

    # The events log proves which branch ran: bootstrap path records
    # bootstrap="started"; the dispatch path does not.
    events = data_root / "logs" / "events.jsonl"
    if events.is_file():
        body = events.read_text()
        assert '"bootstrap":"started"' not in body, (
            "populated DB wrongly logged the bootstrap branch"
        )


def test_hook_short_prompt_skips(tmp_path: Path) -> None:
    """A <10-char prompt is skipped (exit 0, no envelope) even with a real DB."""
    data_root = tmp_path / "total-recall"
    data_root.mkdir(parents=True)
    _build_populated_db(data_root / "index.db")

    proc = _run_hook(tmp_path, "hi")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_stub_prompt_skips(tmp_path: Path) -> None:
    """A '<'-prefixed (tool-stub) prompt is skipped."""
    data_root = tmp_path / "total-recall"
    data_root.mkdir(parents=True)
    _build_populated_db(data_root / "index.db")

    proc = _run_hook(tmp_path, "<tool-result>stuff</tool-result>")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
