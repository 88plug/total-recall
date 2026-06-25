"""Tests for :mod:`lib.sources.grok` — the Grok CLI adapter.

All tests are hermetic — they build a synthetic ``~/.grok/sessions`` tree
under ``tmp_path``. No real ``~/.grok`` is touched.

Grok-CLI on-disk layout (empirically derived from a real corpus on this
machine, ~153 sessions across 5 workspaces)::

    ~/.grok/sessions/
      <url-encoded-cwd>/                 e.g. %2Fhome%2Fandrew%2Frepo
        prompt_history.jsonl             per-workspace, NOT per-session
        <session-uuid>/                  one dir per session (ULID-ish v7 UUID)
          chat_history.jsonl             canonical conversation stream
          summary.json                   session metadata (cwd, title, model, ts)
          events.jsonl                   turn/loop/phase telemetry (not ingested)
          updates.jsonl                  streaming deltas (not ingested)
          rewind_points.jsonl            file snapshots (not ingested)
          signals.json                   aggregate counters (not ingested)
          system_prompt.txt
          terminal/

``chat_history.jsonl`` record shapes (one JSON object per line, blank lines
tolerated)::

    {"type":"system","content":"<system prompt str>"}
    {"type":"user","content":[{"type":"text","text":"..."}]}
    {"type":"reasoning","id":"rs_...","summary":[{"type":"summary_text","text":"..."}],
        "encrypted_content":"<base64>"}
    {"type":"assistant","content":"<str>",
        "tool_calls":[{"id":"call-...","name":"Read","arguments":"<JSON-string>"}],
        "model_id":"grok-composer-2.5-fast","model_fingerprint":"fp_..."}
    {"type":"tool_result","tool_call_id":"call-...","content":"<str>"}

Key shape facts the adapter must handle (all confirmed against the real
corpus):

* ``assistant.content`` is **always a string** (never a block list); tool
  calls live in a *sibling* ``tool_calls`` array, and each call's
  ``arguments`` is a **JSON-encoded string** that needs a second
  ``json.loads`` (same quirk as Codex FunctionCall).
* ``tool_result`` is its own **top-level** record type keyed by
  ``tool_call_id`` (not nested inside a user message as in Anthropic wire
  format).
* ``reasoning`` carries human-readable text under ``summary[].text`` plus
  opaque ``encrypted_content``.
* ``cwd`` is authoritative from ``summary.json``'s ``info.cwd``; the
  url-encoded workspace directory name (``%2Fhome%2F...``) is the fallback
  and must be ``urllib.parse.unquote``'d.
* ``started_at`` comes from ``summary.json``'s ``created_at`` ISO ts.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from lib.schema import AssistantRecord, UserRecord
from lib.sources import SOURCES, all_sources, source_by_name
from lib.sources.grok import GrokSource

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o))
            f.write("\n")


def _write_json(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _summary(sid: str, cwd: str, *, title: str = "", model: str = "grok-composer-2.5-fast") -> dict:
    return {
        "info": {"id": sid, "cwd": cwd},
        "session_summary": title,
        "created_at": "2026-06-15T12:16:24.884900078Z",
        "updated_at": "2026-06-15T12:18:08.748427406Z",
        "num_messages": 4,
        "num_chat_messages": 4,
        "current_model_id": model,
        "session_kind": "primary",
        "generated_title": title,
    }


def _assistant(content: str, tool_calls: list[dict] | None = None,
               model: str = "grok-composer-2.5-fast") -> dict:
    rec = {"type": "assistant", "content": content, "model_id": model}
    if tool_calls is not None:
        rec["tool_calls"] = tool_calls
    return rec


def _make_grok_tree(tmp_path: Path) -> Path:
    """Return a ``~/.grok/sessions``-style root with two sessions.

    Workspace dir name is the url-encoded cwd, mirroring the real layout.
    """
    sessions = tmp_path / ".grok" / "sessions"
    cwd_real = "/home/u/repo"
    ws = sessions / urllib.parse.quote(cwd_real, safe="")

    # --- Session 1: full stream (user / reasoning / assistant+tool_calls / tool_result). ---
    s1 = ws / "019ecb36-291c-77c0-979c-05e50c08a620"
    _write_jsonl(
        s1 / "chat_history.jsonl",
        [
            {"type": "system", "content": "You are an AI coding assistant."},
            {"type": "user", "content": [{"type": "text", "text": "fix the bug"}]},
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "let me look at generator.py"}],
                "encrypted_content": "OPAQUEBYTES==",
            },
            _assistant(
                "I'll read the file.",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "Read",
                        "arguments": json.dumps({"path": "/repo/gen.py"}),
                    }
                ],
            ),
            {"type": "tool_result", "tool_call_id": "call-1", "content": "file contents here"},
            _assistant("patched"),
        ],
    )
    _write_json(
        s1 / "summary.json",
        _summary(
            "019ecb36-291c-77c0-979c-05e50c08a620",
            cwd_real,
            title="fix the bug",
        ),
    )

    # --- Session 2: minimal — single user prompt, no summary title/model. ---
    s2 = ws / "019ec19d-3ac0-7393-b64b-3fedf3524cbb"
    _write_jsonl(
        s2 / "chat_history.jsonl",
        [{"type": "user", "content": [{"type": "text", "text": "just hi"}]}],
    )
    _write_json(s2 / "summary.json", _summary("019ec19d-3ac0-7393-b64b-3fedf3524cbb", cwd_real))

    # Per-workspace prompt_history.jsonl — present but not a session itself.
    _write_jsonl(
        ws / "prompt_history.jsonl",
        [
            {
                "timestamp": "2026-06-15T12:16:24.438312703Z",
                "session_id": "019ecb36-291c-77c0-979c-05e50c08a620",
                "prompt": "fix the bug",
                "is_bash": False,
            }
        ],
    )

    return sessions


# ---------------------------------------------------------------------------
# Identity / availability
# ---------------------------------------------------------------------------


def test_name_constant():
    assert GrokSource.name == "grok"
    assert GrokSource().name == "grok"


def test_default_sessions_root_under_grok_home():
    src = GrokSource()
    assert (Path.home() / ".grok" / "sessions") == src.sessions_root


def test_grok_home_override_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "custom"))
    src = GrokSource()
    assert src.sessions_root == tmp_path / "custom" / "sessions"


def test_explicit_root_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_HOME", "/elsewhere")
    src = GrokSource(sessions_root=tmp_path / "explicit")
    assert src.sessions_root == tmp_path / "explicit"


def test_is_available_false_when_missing(tmp_path):
    assert GrokSource(sessions_root=tmp_path / "nope").is_available() is False


def test_is_available_false_when_empty(tmp_path):
    root = tmp_path / ".grok" / "sessions"
    root.mkdir(parents=True)
    assert GrokSource(sessions_root=root).is_available() is False


def test_is_available_true_with_sessions(tmp_path):
    root = _make_grok_tree(tmp_path)
    assert GrokSource(sessions_root=root).is_available() is True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_yields_session_dirs(tmp_path):
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    sessions = list(src.discover_sessions())

    assert len(sessions) == 2
    assert {s.source for s in sessions} == {"grok"}

    ids = [s.session_id for s in sessions]
    assert ids == sorted(ids)  # stable, sorted ordering for checkpointing

    by_id = {s.session_id: s for s in sessions}
    s1 = by_id["019ecb36-291c-77c0-979c-05e50c08a620"]
    # cwd resolved from summary.json info.cwd
    assert s1.cwd == "/home/u/repo"
    assert s1.extra.get("title") == "fix the bug"
    assert s1.extra.get("model") == "grok-composer-2.5-fast"
    # started_at parsed from summary.json created_at
    assert s1.started_at == pytest.approx(
        # 2026-06-15T12:16:24.884900078Z
        1781525784.8849, abs=1.0
    )
    # canonical handle points at the chat stream
    assert s1.path.name == "chat_history.jsonl"
    assert s1.path.is_file()


def test_discover_skips_prompt_history_as_session(tmp_path):
    """prompt_history.jsonl is a per-workspace sibling, not a session dir."""
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    ids = {s.session_id for s in src.discover_sessions()}
    assert "prompt_history" not in ids
    assert "prompt_history.jsonl" not in ids


def test_discover_skips_session_dir_without_chat_history(tmp_path):
    root = tmp_path / ".grok" / "sessions"
    ws = root / urllib.parse.quote("/home/u/repo", safe="")
    # a session dir with only telemetry, no chat_history.jsonl
    empty = ws / "019ec000-0000-0000-0000-000000000000"
    _write_jsonl(empty / "events.jsonl", [{"ts": "x", "type": "loop_started"}])
    src = GrokSource(sessions_root=root)
    assert list(src.discover_sessions()) == []


def test_discover_falls_back_to_url_decoded_cwd_when_no_summary(tmp_path):
    """No summary.json — cwd comes from url-decoding the workspace dir name."""
    root = tmp_path / ".grok" / "sessions"
    cwd_real = "/home/u/my repo+special"  # space and + survive url round-trip
    ws = root / urllib.parse.quote(cwd_real, safe="")
    s = ws / "019ecdef-0000-0000-0000-000000000abc"
    _write_jsonl(
        s / "chat_history.jsonl",
        [{"type": "user", "content": [{"type": "text", "text": "hi"}]}],
    )
    src = GrokSource(sessions_root=root)
    sessions = list(src.discover_sessions())
    assert len(sessions) == 1
    sf = sessions[0]
    assert sf.cwd == cwd_real
    assert "title" not in sf.extra  # no summary → no title hint
    assert sf.started_at is None  # no summary → unknown start


def test_discover_merges_multiple_workspaces(tmp_path):
    root = tmp_path / ".grok" / "sessions"
    for cwd, sid in (("/home/u/a", "019aaaaa-0000-0000-0000-00000000000a"),
                     ("/home/u/b", "019bbbbb-0000-0000-0000-00000000000b")):
        ws = root / urllib.parse.quote(cwd, safe="")
        _write_jsonl(
            ws / sid / "chat_history.jsonl",
            [{"type": "user", "content": [{"type": "text", "text": "x"}]}],
        )
        _write_json(ws / sid / "summary.json", _summary(sid, cwd))
    src = GrokSource(sessions_root=root)
    ids = {s.session_id for s in src.discover_sessions()}
    assert ids == {
        "019aaaaa-0000-0000-0000-00000000000a",
        "019bbbbb-0000-0000-0000-00000000000b",
    }


# ---------------------------------------------------------------------------
# Record streaming
# ---------------------------------------------------------------------------


def test_iter_records_projects_canonical_records(tmp_path):
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    s1 = next(
        s for s in src.discover_sessions()
        if s.session_id == "019ecb36-291c-77c0-979c-05e50c08a620"
    )
    records = [r for _, r in src.iter_records(s1)]

    # system prompt + reasoning are folded/skipped or projected; the
    # conversation-bearing records below must all be present in order.
    user = next(r for r in records if isinstance(r, UserRecord) and r.content_kind == "string")
    assert user.text == "fix the bug"
    assert user.cwd == "/home/u/repo"
    assert user.session_id == "019ecb36-291c-77c0-979c-05e50c08a620"

    # assistant with text + a tool_use block synthesised from tool_calls[]
    asst = next(
        r for r in records
        if isinstance(r, AssistantRecord) and any(b.type == "tool_use" for b in r.content)
    )
    assert asst.model == "grok-composer-2.5-fast"
    types = [b.type for b in asst.content]
    assert "text" in types and "tool_use" in types
    tu = next(b.tool_use for b in asst.content if b.type == "tool_use")
    assert tu.id == "call-1"
    assert tu.name == "Read"
    # arguments JSON-string was double-decoded into a dict
    assert tu.input == {"path": "/repo/gen.py"}

    # tool_result projected as a UserRecord(tool_result) keyed by tool_call_id
    tr_rec = next(
        r for r in records
        if isinstance(r, UserRecord) and r.content_kind == "tool_result"
    )
    assert len(tr_rec.tool_results) == 1
    assert tr_rec.tool_results[0].tool_use_id == "call-1"
    assert tr_rec.tool_results[0].content == "file contents here"


def test_iter_records_reasoning_becomes_thinking_block(tmp_path):
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    s1 = next(
        s for s in src.discover_sessions()
        if s.session_id == "019ecb36-291c-77c0-979c-05e50c08a620"
    )
    records = [r for _, r in src.iter_records(s1)]
    thinking = [
        b
        for r in records
        if isinstance(r, AssistantRecord)
        for b in r.content
        if b.type == "thinking"
    ]
    assert thinking, "reasoning record should project a thinking block"
    assert any("generator.py" in (b.thinking or "") for b in thinking)


def test_iter_records_assistant_without_tool_calls(tmp_path):
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    s1 = next(
        s for s in src.discover_sessions()
        if s.session_id == "019ecb36-291c-77c0-979c-05e50c08a620"
    )
    records = [r for _, r in src.iter_records(s1)]
    # the final "patched" assistant turn — text only, no tool_use
    final = [
        r for r in records
        if isinstance(r, AssistantRecord)
        and any(b.type == "text" and b.text == "patched" for b in r.content)
    ]
    assert final
    assert all(b.type != "tool_use" for b in final[0].content)


def test_iter_records_respects_start_offset(tmp_path):
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    s1 = next(
        s for s in src.discover_sessions()
        if s.session_id == "019ecb36-291c-77c0-979c-05e50c08a620"
    )
    full = list(src.iter_records(s1))
    assert len(full) >= 2
    mid_offset = full[0][0]
    tail = list(src.iter_records(s1, start_offset=mid_offset))
    assert len(tail) == len(full) - 1


def test_iter_records_tolerates_blank_lines(tmp_path):
    root = tmp_path / ".grok" / "sessions"
    ws = root / urllib.parse.quote("/home/u/repo", safe="")
    s = ws / "019ecdef-0000-0000-0000-00000000blank"
    s.mkdir(parents=True)
    with (s / "chat_history.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "content": [{"type": "text", "text": "hi"}]}) + "\n")
        f.write("\n")  # blank line must not raise
        f.write(json.dumps(_assistant("ok")) + "\n")
    _write_json(
        s / "summary.json",
        _summary("019ecdef-0000-0000-0000-00000000blank", "/home/u/repo"),
    )
    src = GrokSource(sessions_root=root)
    sf = next(iter(src.discover_sessions()))
    recs = [r for _, r in src.iter_records(sf)]
    assert any(isinstance(r, UserRecord) for r in recs)
    assert any(isinstance(r, AssistantRecord) for r in recs)


def test_iter_records_malformed_lines_skipped(tmp_path):
    root = tmp_path / ".grok" / "sessions"
    ws = root / urllib.parse.quote("/home/u/repo", safe="")
    s = ws / "019ecdef-0000-0000-0000-0000000bad00"
    s.mkdir(parents=True)
    with (s / "chat_history.jsonl").open("w", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write(json.dumps({"type": "user", "content": [{"type": "text", "text": "ok"}]}) + "\n")
    _write_json(
        s / "summary.json",
        _summary("019ecdef-0000-0000-0000-0000000bad00", "/home/u/repo"),
    )
    src = GrokSource(sessions_root=root)
    sf = next(iter(src.discover_sessions()))
    recs = [r for _, r in src.iter_records(sf)]
    # the one good line still parses
    assert any(isinstance(r, UserRecord) and r.text == "ok" for r in recs)


def test_iter_records_bad_tool_call_arguments_preserved(tmp_path):
    """Non-JSON ``arguments`` must not crash — fall back gracefully."""
    root = tmp_path / ".grok" / "sessions"
    ws = root / urllib.parse.quote("/home/u/repo", safe="")
    s = ws / "019ecdef-0000-0000-0000-00000badargs"
    _write_jsonl(
        s / "chat_history.jsonl",
        [_assistant("calling", tool_calls=[
            {"id": "c9", "name": "Shell", "arguments": "not-json"},
        ])],
    )
    _write_json(
        s / "summary.json",
        _summary("019ecdef-0000-0000-0000-00000badargs", "/home/u/repo"),
    )
    src = GrokSource(sessions_root=root)
    sf = next(iter(src.discover_sessions()))
    rec = next(
        r for _, r in src.iter_records(sf)
        if isinstance(r, AssistantRecord) and any(b.type == "tool_use" for b in r.content)
    )
    tu = next(b.tool_use for b in rec.content if b.type == "tool_use")
    assert tu.name == "Shell"
    # input is a dict regardless (raw string stashed, never a crash)
    assert isinstance(tu.input, dict)


def test_iter_records_missing_timestamp_yields_none_ts(tmp_path):
    """chat_history records carry no per-line timestamp — ts must be None,
    not a crash, and never falls back to 'now'."""
    root = _make_grok_tree(tmp_path)
    src = GrokSource(sessions_root=root)
    s1 = next(
        s for s in src.discover_sessions()
        if s.session_id == "019ecb36-291c-77c0-979c-05e50c08a620"
    )
    records = [r for _, r in src.iter_records(s1)]
    assert all(r.ts is None for r in records)


def test_iter_records_empty_chat_history_yields_nothing(tmp_path):
    root = tmp_path / ".grok" / "sessions"
    ws = root / urllib.parse.quote("/home/u/repo", safe="")
    s = ws / "019ecdef-0000-0000-0000-000000empty0"
    _write_jsonl(s / "chat_history.jsonl", [])
    _write_json(
        s / "summary.json",
        _summary("019ecdef-0000-0000-0000-000000empty0", "/home/u/repo"),
    )
    src = GrokSource(sessions_root=root)
    sf = next(iter(src.discover_sessions()))
    assert list(src.iter_records(sf)) == []


def test_iter_records_non_ascii_cwd(tmp_path):
    """Url-encoded workspace dir with non-ASCII chars decodes correctly."""
    root = tmp_path / ".grok" / "sessions"
    cwd_real = "/home/u/projét-日本"
    ws = root / urllib.parse.quote(cwd_real, safe="")
    sid = "019ecdef-0000-0000-0000-0000nonascii"
    _write_jsonl(
        ws / sid / "chat_history.jsonl",
        [{"type": "user", "content": [{"type": "text", "text": "bonjour"}]}],
    )
    # No summary.json → forces the url-decode fallback path for cwd.
    src = GrokSource(sessions_root=root)
    sf = next(iter(src.discover_sessions()))
    assert sf.cwd == cwd_real
    rec = next(r for _, r in src.iter_records(sf) if isinstance(r, UserRecord))
    assert rec.cwd == cwd_real
    assert rec.text == "bonjour"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_adapter_in_registry_after_import():
    import lib.sources.grok  # noqa: F401
    assert GrokSource in SOURCES


def test_source_by_name_returns_grok():
    import lib.sources.grok  # noqa: F401
    src = source_by_name("grok")
    assert isinstance(src, GrokSource)


def test_all_sources_includes_grok():
    import lib.sources.grok  # noqa: F401
    assert any(isinstance(s, GrokSource) for s in all_sources())
