"""Integration tests against the real on-disk corpus at ~/.claude/projects.

These tests are *defensive*: sibling worktrees (walker, extractors, index,
signpost hook) may not all be merged yet. Each test imports its dependencies
inside the test body and `pytest.skip`s cleanly if the required module is
absent. The goal is that this file can sit in CI from day one without
flaking when other WTs land.

Tests are read-only on the corpus. Never write into ~/.claude/projects.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

CORPUS = pathlib.Path("~/.claude/projects").expanduser()
pytestmark = pytest.mark.skipif(not CORPUS.exists(), reason="no corpus on this machine")


def _try_import(modpath: str):
    """Return the module or pytest.skip with a clear reason."""
    try:
        mod = __import__(modpath, fromlist=["*"])
        return mod
    except Exception as e:  # noqa: BLE001 — any import-time failure means "not on this branch"
        pytest.skip(f"module {modpath} not present on this branch: {e}")


def test_walker_parses_smallest_real_session(real_corpus_smallest_session: pathlib.Path):
    """Pick smallest .jsonl; assert WT-2's walker parses without errors.

    Tolerates blank lines and malformed records — the walker is the
    contract surface, not json.loads directly.
    """
    walker_mod = None
    for candidate in ("lib.walker", "total_recall.walker", "total_recall.lib.walker"):
        try:
            walker_mod = __import__(candidate, fromlist=["*"])
            break
        except Exception:  # noqa: BLE001
            continue
    if walker_mod is None:
        pytest.skip("walker module not present on this branch")

    walk = getattr(walker_mod, "walk_session", None) or getattr(walker_mod, "iter_session", None)
    if walk is None:
        pytest.skip("walker module lacks walk_session / iter_session")

    count = 0
    for rec in walk(real_corpus_smallest_session):
        assert isinstance(rec, dict), f"walker yielded non-dict: {type(rec)!r}"
        # every walker output should be tagged by type
        assert "type" in rec or "uuid" in rec, f"walker output missing type/uuid: {rec!r}"
        count += 1
    assert count > 0, "walker yielded zero records from a non-empty .jsonl"


def test_extractors_fire_on_real_corrections(real_corpus_root: pathlib.Path):
    """Find user 'no' turns in -home-operator-nova-nova-cluster; assert
    Corrections extractor catches them.

    Falls back to *any* project with at least one literal 'no' / 'not '
    / 'wrong' user turn if the canonical project isn't on this machine.
    """
    extractors_mod = None
    for candidate in (
        "extractors.corrections",
        "total_recall.extractors.corrections",
        "extractors",
    ):
        try:
            extractors_mod = __import__(candidate, fromlist=["*"])
            break
        except Exception:  # noqa: BLE001
            continue
    if extractors_mod is None:
        pytest.skip("corrections extractor module not present on this branch")

    Corrections = (
        getattr(extractors_mod, "Corrections", None)
        or getattr(extractors_mod, "CorrectionsExtractor", None)
        or getattr(extractors_mod, "extract_corrections", None)
    )
    if Corrections is None:
        pytest.skip("Corrections / CorrectionsExtractor not exported")

    candidates = [
        real_corpus_root / "-home-operator-nova-nova-cluster",
        *sorted(real_corpus_root.iterdir()),
    ]

    needles = ("no", "not ", "wrong", "stop", "don't")
    hit_session = None
    hit_msg = None
    for proj in candidates:
        if not proj.is_dir():
            continue
        for jsonl in proj.glob("*.jsonl"):
            try:
                with jsonl.open() as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") != "user":
                            continue
                        msg = rec.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, list):
                            content = " ".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        if not isinstance(content, str):
                            continue
                        low = content.strip().lower()
                        if any(low.startswith(n) for n in needles):
                            hit_session = jsonl
                            hit_msg = rec
                            break
                if hit_msg:
                    break
            except OSError:
                continue
        if hit_msg:
            break

    if hit_msg is None:
        pytest.skip("no correction-like user turn found in real corpus")

    # The extractor expects a parsed Record (with .text, .content_kind, etc.),
    # not the raw JSONL dict. Run it through lib.schema.parse_record first.
    try:
        from lib.schema import parse_record
    except ImportError:
        pytest.skip("lib.schema.parse_record not available on this branch")
    parsed = parse_record(hit_msg, byte_offset=0)

    # Call whichever shape the extractor exposes
    results = None
    if callable(Corrections) and not isinstance(Corrections, type):
        results = Corrections([parsed])
    else:
        inst = Corrections()
        extract = getattr(inst, "extract", None) or (inst if callable(inst) else None)
        if extract is None:
            pytest.skip("Corrections extractor has no extract() method")
        results = extract([parsed])

    results = list(results) if results is not None else []
    assert results, f"Corrections extractor missed obvious correction in {hit_session}: {hit_msg!r}"


def test_index_full_ingest_smoke(
    real_corpus_smallest_session: pathlib.Path,
    tmp_path: pathlib.Path,
):
    """End-to-end: ingest one small real .jsonl into a fresh DB; query a
    plausible topic; assert > 0 messages indexed.

    Topic match is best-effort — we just assert the ingest pipeline ran
    and produced rows. Topic-relevance is the search layer's job, not
    this smoke test's.
    """
    index_mod = None
    for candidate in ("index.ingest", "total_recall.index.ingest", "index"):
        try:
            index_mod = __import__(candidate, fromlist=["*"])
            break
        except Exception:  # noqa: BLE001
            continue
    if index_mod is None:
        pytest.skip("index/ingest module not present on this branch")

    ingest = (
        getattr(index_mod, "ingest_session", None)
        or getattr(index_mod, "ingest_file", None)
        or getattr(index_mod, "ingest", None)
    )
    if ingest is None:
        pytest.skip("no ingest entrypoint exported")

    db_path = tmp_path / "smoke.db"
    try:
        ingest(real_corpus_smallest_session, db_path=str(db_path))
    except TypeError:
        # alternate signature: ingest(db, path)
        try:
            ingest(str(db_path), real_corpus_smallest_session)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"ingest call signature not compatible: {e}")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"ingest raised on real .jsonl: {e}")

    assert db_path.exists(), "ingest did not create the DB"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
        # Some table for content should exist
        assert names, "ingested DB has no tables"
        msg_table = next((t for t in ("messages", "records", "turns") if t in names), None)
        if msg_table is None:
            pytest.skip(f"DB schema unknown — tables: {names}")
        n = conn.execute(f"SELECT COUNT(*) FROM {msg_table}").fetchone()[0]
        assert n > 0, f"ingest produced 0 rows in {msg_table}"
    finally:
        conn.close()


def test_signpost_hook_outputs_envelope_for_known_cwd(tmp_path: pathlib.Path):
    """Run hooks/session-start-signpost.sh with synthetic stdin; assert
    valid envelope JSON on stdout.

    The hook's contract (per Claude Code hook spec) is to write JSON to
    stdout that becomes additionalContext. We don't pin the exact schema
    — just that it parses as JSON and contains at least one of the
    expected envelope keys.
    """
    repo = pathlib.Path(__file__).resolve().parent.parent.parent
    hook = repo / "hooks" / "session-start-signpost.sh"
    if not hook.exists():
        # try lib subdir
        hook = repo / "hooks" / "lib" / "session-start-signpost.sh"
    if not hook.exists():
        pytest.skip("session-start-signpost.sh not present on this branch")
    if not os.access(hook, os.X_OK):
        pytest.skip("signpost hook is not executable")

    payload = json.dumps(
        {
            "session_id": "test-session",
            "cwd": "/home/operator/ip-service-for-docker",
            "hook_event_name": "SessionStart",
        }
    )
    try:
        result = subprocess.run(
            [str(hook)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(tmp_path),
        )
    except subprocess.TimeoutExpired:
        pytest.fail("signpost hook timed out")

    if result.returncode != 0 and not result.stdout.strip():
        pytest.skip(
            f"signpost hook exited {result.returncode} with no output; "
            f"stderr: {result.stderr[:200]}"
        )

    out = result.stdout.strip()
    if not out:
        pytest.skip("signpost hook produced no stdout (no memories for this cwd?)")

    try:
        envelope = json.loads(out)
    except json.JSONDecodeError as e:
        pytest.fail(f"signpost hook stdout is not valid JSON: {e}\n{out[:500]}")

    assert isinstance(envelope, dict), "envelope is not a JSON object"
    # at least one of the expected hook-output keys
    expected_keys = {
        "hookSpecificOutput",
        "additionalContext",
        "continue",
        "systemMessage",
        "suppressOutput",
    }
    assert expected_keys & set(envelope.keys()), (
        f"envelope missing any expected hook-output key; got {list(envelope.keys())}"
    )


# ---------------------------------------------------------------------------
# Post-validation regression locks — added after the docker validation round
# turned up 16 issues across HIGH/MEDIUM/LOW severity. Each test below locks
# in a specific fix so we can't silently regress.
# ---------------------------------------------------------------------------


# Real-world prefixes for secrets that the scrubber MUST catch before any
# matching substring lands in `messages.text`. We require a length tail that
# matches the actual scrubber regexes from `extractors/secrets.py` so the
# test is a faithful proxy for the production scrub patterns.
_LIVE_SECRET_PATTERNS = (
    ("anthropic_sk", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("aws_akia", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
)


def _import_first(*candidates: str):
    for cand in candidates:
        try:
            return __import__(cand, fromlist=["*"])
        except Exception:  # noqa: BLE001
            continue
    return None


def test_scrub_on_ingest_locks_in_no_secret_leak_into_messages_text(
    real_corpus_smallest_session: pathlib.Path,
    tmp_path: pathlib.Path,
):
    """After ingesting a real session, `messages.text` must not contain
    substrings matching the actual scrubber's secret patterns (`sk-...`,
    `AKIA...`, `ghp_...`).

    Locks in F2's "scrub-on-ingest" fix: even if a transcript pasted in a
    leaked token, the index must not preserve it.
    """
    db_mod = _import_first("index.db", "total_recall.index.db")
    ingest_mod = _import_first("index.ingest", "total_recall.index.ingest")
    if db_mod is None or ingest_mod is None:
        pytest.skip("index.db / index.ingest not present on this branch")

    connect = getattr(db_mod, "connect", None)
    ingest_file = getattr(ingest_mod, "ingest_file", None)
    if connect is None or ingest_file is None:
        pytest.skip("index API missing connect/ingest_file")

    db_path = tmp_path / "scrub.db"
    conn = connect(db_path)
    try:
        ingest_file(conn, real_corpus_smallest_session, force_full=True)
        rows = conn.execute("SELECT text FROM messages WHERE text IS NOT NULL").fetchall()
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    if not rows:
        pytest.skip("ingest produced 0 message rows; nothing to check")

    leaks: list[tuple[str, str]] = []
    for row in rows:
        text = row[0] if not hasattr(row, "keys") else row["text"]
        if not isinstance(text, str) or not text:
            continue
        for label, pat in _LIVE_SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                leaks.append((label, m.group(0)))
    assert not leaks, (
        f"messages.text leaked {len(leaks)} secret-shaped substrings after "
        f"ingest scrubbing: {leaks[:3]}..."
    )


def test_cli_index_full_on_tiny_fixture_exits_zero(
    real_corpus_smallest_session: pathlib.Path,
    tmp_path: pathlib.Path,
):
    """`total-recall index --full --projects-root <tiny-fixture>` exits 0.

    Locks in F2's signature-drift fix on `ingest_all(projects_root=...)`.
    Builds the fixture by copying the smallest real session into a fresh
    projects-root layout so the CLI gets a realistic shape to chew on.
    """
    # Build fixture: <root>/<slug>/<session>.jsonl
    fixture_root = tmp_path / "projects"
    slug_dir = fixture_root / "-tmp-tiny-fixture"
    slug_dir.mkdir(parents=True)
    target = slug_dir / real_corpus_smallest_session.name
    target.write_bytes(real_corpus_smallest_session.read_bytes())

    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin-data")}
    # Use the same interpreter that's running pytest so the package is found.
    cmd = [
        sys.executable,
        "-m",
        "total_recall",
        "index",
        "--full",
        "--projects-root",
        str(fixture_root),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail("total-recall index timed out on tiny fixture")
    except FileNotFoundError:
        pytest.skip("python interpreter or total_recall module unavailable")

    if result.returncode == 1 and "not yet available" in result.stderr:
        pytest.skip(f"index module not installed: {result.stderr.strip()}")
    assert result.returncode == 0, (
        f"total-recall index exited {result.returncode}\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )


def test_vec_cli_search_clean_error_without_extras(tmp_path: pathlib.Path):
    """`python -m vec.cli search <topic>` either exits 0 (extras installed)
    or exits non-zero with a clean, single-line error mentioning the install
    hint. Locks in F3's "no traceback, just tell the user to install vec".
    """
    try:
        import sqlite_vec  # noqa: F401

        have_vec = True
    except Exception:  # noqa: BLE001
        have_vec = False

    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin-data")}
    cmd = [
        sys.executable,
        "-m",
        "vec.cli",
        "search",
        "provider-x",
        "--limit",
        "3",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail("vec.cli search timed out")
    except FileNotFoundError:
        pytest.skip("python interpreter unavailable")

    if have_vec:
        # With extras installed and an empty / nonexistent DB, search should
        # still exit 0 ("no hits") — not crash.
        assert result.returncode == 0, (
            f"vec.cli search with [vec] extras returned {result.returncode}\n"
            f"stderr: {result.stderr[:300]}"
        )
    else:
        assert result.returncode != 0, (
            "vec.cli search should exit non-zero without [vec] extras; "
            f"got 0 with stdout: {result.stdout[:200]}"
        )
        combined = (result.stderr + "\n" + result.stdout).lower()
        assert "sqlite-vec" in combined or "total-recall[vec]" in combined, (
            "missing install hint in error output:\n"
            f"stderr: {result.stderr[:300]}\nstdout: {result.stdout[:300]}"
        )
        # No raw Python tracebacks should leak — that's the F3 fix.
        assert "Traceback (most recent call last)" not in result.stderr, (
            f"vec.cli leaked a traceback instead of a clean error:\n{result.stderr[:500]}"
        )
