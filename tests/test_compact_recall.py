"""Post-compaction recall text stays empty without an index and marks real hits."""

from __future__ import annotations

from pathlib import Path

from index.db import connect
from index.paths import project_key
from total_recall.compact_recall import render_compact_recall


def test_missing_db_is_silent(tmp_path: Path) -> None:
    assert render_compact_recall(db_path=tmp_path / "missing.db", cwd="/proj") == ""


def test_renders_operator_context_and_hit(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "index.db"
    conn = connect(db)
    cwd = "/proj/recall"
    try:
        conn.execute(
            "INSERT INTO extractions (kind, content, session_id, cwd, ts, source_uuid, "
            "score, project_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "decision",
                "ship the parser fix",
                "s1",
                cwd,
                1_700_000_000,
                "u1",
                0.9,
                project_key(cwd),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    import mcp_server.extras.operator_context_tools as operator_tools

    monkeypatch.setattr(
        operator_tools,
        "get_operator_context",
        lambda cwd=None, max_chars=4000: {
            "identity": {"name": "Ada", "handle": "ada"},
            "bans": [{"target": "cloudflared", "reason": "never a tenant hostname"}],
            "standing_decisions": [{"topic": "forge", "chose": "refs", "over": "gitlab"}],
        },
    )
    text = render_compact_recall(db_path=db, cwd=cwd, topic="parser")
    assert "POST-COMPACTION RECALL" in text
    assert "END RECALL" in text
    assert "Operator: Ada @ada" in text
    assert "Ban: cloudflared" in text
    assert "Decision: forge = refs, not gitlab" in text
    assert "ship the parser fix" in text


def test_topic_comes_from_continuation_packet(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "index.db"
    connect(db).close()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sid.continuation.json").write_text(
        '{"last_user_directive":"fix parser.py","_kind":"continuation_packet"}',
        encoding="utf-8",
    )
    seen: dict[str, str] = {}

    def _no_operator(cwd=None, max_chars=1800):
        return {}

    import mcp_server.extras.operator_context_tools as operator_tools

    monkeypatch.setattr(operator_tools, "get_operator_context", _no_operator)

    def _capture(conn, query=None, cwd=None, limit=3, **kwargs):
        seen.setdefault("query", query or "")
        return []

    monkeypatch.setattr("index.query.search_extractions", _capture)
    # render imports search_extractions inside the call, so patch the module
    # attribute before the call. The inner import binds the patched function.
    text = render_compact_recall(db_path=db, cwd="/proj", session_id="sid")
    assert text == ""
    assert seen["query"] == "fix parser.py"
