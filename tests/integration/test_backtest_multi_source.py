"""Multi-source backtest — synthetic, no real operator PII.

Proves that:
1. The ingest layer tags rows with the correct ``source`` value for every
   CLI adapter (claude_code, opencode, codex).
2. The cross-source dedup heuristic in :mod:`index.multi_source` suppresses
   the lower-priority duplicate when the same content appears in two sources.
3. The operator-profile extractor sees and mines records from non-claude_code
   sources — i.e. a fact that only appears in an opencode session still ends
   up in the extracted profile.
4. (Gated) On a machine that actually has non-claude_code sessions on disk,
   ``ingest_all`` produces rows with ``source != 'claude_code'``.

Synthetic persona: Dana Vo, machine oc-box.
No real operator name / hostname / email appears anywhere in this file.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers — build synthetic Record-like objects
# ---------------------------------------------------------------------------

# We construct minimal record-like objects that satisfy what the extractor
# and ingest layer need:
#   * .type (str)
#   * .text (str | None)  -- for user records
#   * .content (list[Block]) -- for assistant records
#   * .session_id, .cwd, .uuid, .byte_offset, .ts, .source_file
# The lib.schema.Record dataclass itself is used so the objects are real
# Records, not plain dicts — this exercises the same code path that live
# adapters hit.


def _make_user_record(
    text: str,
    *,
    session_id: str = "sess-dana-01",
    cwd: str = "/home/dana/proj",
    uuid: str | None = None,
    ts: datetime | None = None,
) -> Any:
    from lib.schema import UserRecord

    return UserRecord(
        type="user",
        uuid=uuid or f"uuid-u-{abs(hash(text)) % 100000}",
        parent_uuid=None,
        session_id=session_id,
        ts=ts or datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        cwd=cwd,
        git_branch="main",
        version="test",
        is_sidechain=False,
        raw={},
        byte_offset=0,
        content_kind="string",
        text=text,
    )


def _make_assistant_record(
    text: str,
    *,
    session_id: str = "sess-dana-01",
    cwd: str = "/home/dana/proj",
    uuid: str | None = None,
    ts: datetime | None = None,
) -> Any:
    from lib.schema import AssistantRecord, Block

    block = Block(type="text", text=text)
    return AssistantRecord(
        type="assistant",
        uuid=uuid or f"uuid-a-{abs(hash(text)) % 100000}",
        parent_uuid=None,
        session_id=session_id,
        ts=ts or datetime(2026, 5, 1, 12, 1, 0, tzinfo=timezone.utc),
        cwd=cwd,
        git_branch="main",
        version="test",
        is_sidechain=False,
        raw={},
        byte_offset=0,
        content=[block],
    )


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _open_fresh_db() -> sqlite3.Connection:
    """Open an in-memory DB with the full schema applied."""
    from index.db import connect

    return connect(Path(":memory:"))


# ---------------------------------------------------------------------------
# Test 1 — per-source tagging
# ---------------------------------------------------------------------------


def test_synthetic_per_source_tagging() -> None:
    """Ingesting synthetic records tagged for three sources produces three
    distinct ``source`` values in ``messages``, and the non-CC row counts
    are each > 0.
    """
    from index.ingest import _ParsedFile, _commit_parsed, _row_for_message

    conn = _open_fresh_db()

    sources_and_records = [
        ("claude_code", [
            _make_user_record("user asked something", session_id="sess-cc"),
            _make_assistant_record("assistant replied", session_id="sess-cc"),
        ]),
        ("opencode", [
            _make_user_record(
                "opencode user turn",
                session_id="sess-oc",
                cwd="/home/dana/oc-proj",
            ),
            _make_assistant_record(
                "opencode assistant turn",
                session_id="sess-oc",
                cwd="/home/dana/oc-proj",
            ),
        ]),
        ("codex", [
            _make_user_record(
                "codex user message",
                session_id="sess-cx",
                cwd="/home/dana/cx-proj",
            ),
        ]),
    ]

    for source, records in sources_and_records:
        msg_rows = [_row_for_message(r, f"fake://{source}", source=source) for r in records]
        parsed = _ParsedFile(
            source_file=f"fake://{source}",
            inode=0,
            size=len(records),
            mtime=0,
            rotated=False,
            start_offset=0,
            message_rows=msg_rows,
            source=source,
        )
        _commit_parsed(conn, parsed)

    # All three sources must appear.
    sources_in_db = {
        row[0]
        for row in conn.execute("SELECT DISTINCT source FROM messages").fetchall()
    }
    assert sources_in_db == {"claude_code", "opencode", "codex"}, (
        f"Expected all three sources; got {sources_in_db}"
    )

    # Non-CC row counts must each be > 0.
    for src in ("opencode", "codex"):
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE source = ?", (src,)
        ).fetchone()[0]
        assert count > 0, f"Expected >0 rows for source={src!r}, got {count}"


# ---------------------------------------------------------------------------
# Test 2 — cross-source dedup
# ---------------------------------------------------------------------------


def test_synthetic_cross_source_dedup() -> None:
    """When opencode and codex contain the same text at the same cwd + minute,
    ``filter_dedup_rows`` suppresses the lower-priority source (codex ranks
    higher than opencode in SOURCE_PRIORITY, so the opencode row is suppressed).

    Priority order (lower index = higher priority):
        claude_code(0) > codex(1) > opencode(2)

    So if codex and opencode share a dedup key, the opencode row is suppressed.
    """
    from index.ingest import _row_for_message
    from index.multi_source import SOURCE_PRIORITY, filter_dedup_rows

    # Verify the assumed priority order holds.
    assert SOURCE_PRIORITY.index("codex") < SOURCE_PRIORITY.index("opencode"), (
        "Test assumption violated: codex should outrank opencode in SOURCE_PRIORITY"
    )

    shared_text = "shared content that appears in both codex and opencode"
    shared_ts = datetime(2026, 5, 1, 14, 30, 0, tzinfo=timezone.utc)
    cwd = "/home/dana/shared-proj"

    codex_record = _make_user_record(
        shared_text,
        session_id="sess-codex-dedup",
        cwd=cwd,
        uuid="uuid-codex-dedup-001",
        ts=shared_ts,
    )
    opencode_record = _make_user_record(
        shared_text,
        session_id="sess-oc-dedup",
        cwd=cwd,
        # Different UUID so it's a different DB row, same content fingerprint.
        uuid="uuid-oc-dedup-001",
        ts=shared_ts,
    )

    # Build rows in the same format _ingest_all_multi_source would produce.
    # message_rows layout: [0]session_id [1]cwd [2]git_branch [3]role [4]kind
    #                       [5]ts [6]parent_uuid [7]message_uuid [8]byte_offset
    #                       [9]source_file [10]text [11]raw_json [12]source
    msg_cwd_idx, msg_ts_idx, msg_text_idx = 1, 5, 10

    codex_rows = [_row_for_message(codex_record, "fake://codex", source="codex")]
    opencode_rows = [_row_for_message(opencode_record, "fake://opencode", source="opencode")]

    # Simulate the multi-source ingest pass: codex first (higher priority),
    # then opencode.
    kept_codex, seen, suppressed_codex = filter_dedup_rows(
        codex_rows,
        source="codex",
        cwd_idx=msg_cwd_idx,
        ts_idx=msg_ts_idx,
        text_idx=msg_text_idx,
        seen=None,
    )
    assert suppressed_codex == 0, "codex row should not be suppressed (it's first)"
    assert len(kept_codex) == 1

    kept_opencode, seen, suppressed_opencode = filter_dedup_rows(
        opencode_rows,
        source="opencode",
        cwd_idx=msg_cwd_idx,
        ts_idx=msg_ts_idx,
        text_idx=msg_text_idx,
        seen=seen,
    )

    assert suppressed_opencode == 1, (
        f"Expected opencode row to be suppressed (codex wins); got suppressed={suppressed_opencode}"
    )
    assert len(kept_opencode) == 0, (
        "Suppressed row should not appear in kept list"
    )
    # The seen dict should still record codex as the winner.
    assert all(v == "codex" for v in seen.values()), (
        "codex should remain the owner of the dedup key after opencode pass"
    )


# ---------------------------------------------------------------------------
# Test 3 — operator profile sees non-cc source (load-bearing regression)
# ---------------------------------------------------------------------------


def test_synthetic_profile_sees_non_cc_source() -> None:
    """A fact appearing ONLY in an opencode session is captured in the
    operator profile.

    Constructs a multi-source record set where:
    - claude_code records contain no identity signals
    - opencode records contain git config user.name "Dana Vo" + ssh usage of oc-box

    Calls extract_operator_profile_from_records over ALL records and asserts
    the profile contains Dana Vo + oc-box.

    This is the load-bearing regression: if multi-source records are not
    fed to the extractor, the opencode-only signals will be missed.
    """
    from extractors.operator_profile import extract_operator_profile_from_records

    # claude_code records: no identity signals.
    cc_records = [
        _make_user_record(
            "let's refactor the module",
            session_id="sess-cc-plain",
            cwd="/home/dana/proj",
        ),
        _make_assistant_record(
            "Sure, I can help with that refactoring.",
            session_id="sess-cc-plain",
            cwd="/home/dana/proj",
        ),
    ]

    # opencode records: contain the identity signals ONLY here.
    # git config user.name "Dana Vo" → triggers _GIT_USER_NAME_RE.
    # ssh oc-box → triggers _HOSTNAME_CONTEXT_RE (ssh context).
    oc_records = [
        _make_user_record(
            'I set my git identity with: git config user.name "Dana Vo" and I ssh oc-box',
            session_id="sess-oc-identity",
            cwd="/home/dana/oc-proj",
        ),
        _make_assistant_record(
            "Understood. Your git user.name is Dana Vo and your machine oc-box is configured.",
            session_id="sess-oc-identity",
            cwd="/home/dana/oc-proj",
        ),
    ]

    # Combine: the extractor receives ALL records regardless of source.
    all_records = cc_records + oc_records

    profile = extract_operator_profile_from_records(all_records)

    # The profile must have captured "Dana Vo" from the opencode session.
    assert profile.name and "Dana" in profile.name and "Vo" in profile.name, (
        f"Expected profile.name to contain 'Dana Vo' (from opencode session); "
        f"got {profile.name!r}"
    )

    # The profile must have captured "oc-box" as a machine.
    # machines is a dict keyed by hostname.
    machine_keys = set(profile.machines.keys())
    assert "oc-box" in machine_keys, (
        f"Expected 'oc-box' in profile.machines; got {machine_keys!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — real machine, non-cc source present (gated)
# ---------------------------------------------------------------------------


def _non_cc_sources_available() -> bool:
    """Return True if any non-claude_code source reports is_available()."""
    try:
        import lib.sources  # noqa: F401 — triggers adapter registration
        from lib.sources.base import all_sources

        for src in all_sources():
            if getattr(src, "name", None) == "claude_code":
                continue
            try:
                if src.is_available():
                    return True
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return False


@pytest.mark.skipif(
    not _non_cc_sources_available(),
    reason="No non-claude_code source is_available() on this machine",
)
def test_real_machine_multi_source(tmp_path: Path, capsys: Any) -> None:
    """Ingest all available sources on the real machine and assert at least
    one non-claude_code message row was indexed.

    Prints per-source row counts to stdout (visible with pytest -s).
    """
    from index.db import connect
    from index.ingest import ingest_all

    db_path = tmp_path / "multi_source_real.db"
    reports = ingest_all(db_path=db_path, sources=None, trigger="test_real_multi_source")

    conn = connect(db_path, read_only=True)
    try:
        non_cc_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE source != 'claude_code'"
        ).fetchone()[0]

        assert non_cc_count > 0, (
            f"Expected COUNT(*) WHERE source!='claude_code' > 0; got {non_cc_count}. "
            f"Reports: {[(r.file, r.new_messages) for r in reports]}"
        )

        # Print per-source breakdown (visible with pytest -s).
        rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM messages GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        with capsys.disabled():
            print("\n[test_real_machine_multi_source] per-source message counts:")
            for row in rows:
                print(f"  source={row[0]!r:20s}  count={row[1]}")
    finally:
        conn.close()
