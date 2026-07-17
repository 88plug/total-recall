"""Tests for the workflow-profile extractor + storage layer.

Strategy:
- Build a synthetic 3-session corpus exercising each field.
- Assert directional correctness for an action-bias-shaped operator:
  fan-out hits > 0, autonomy > 0.5, non-empty vocabulary, etc.
- Assert empty-corpus → all-zero / empty defaults (stable shape).
- Assert SQLite round-trip preserves lists and floats.
- Assert incremental EMA blends towards the batch for large batches and
  drifts gently for single-session batches.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from extractors.workflow import (
    WorkflowProfile,
    extract_workflow,
    extract_workflow_incremental,
)
from index.workflow import (
    ensure_schema,
    get_workflow,
    persist_workflow,
)

# ---------------------------------------------------------------------------
# JSONL corpus builder helpers
# ---------------------------------------------------------------------------


def _user(session_id: str, text: str, ts: str = "2025-05-01T20:00:00.000Z") -> dict:
    """Minimal user turn — raw JSONL shape (message.content = str)."""
    return {
        "type": "user",
        "sessionId": session_id,
        "uuid": f"{session_id}-u-{text[:8]}",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant_task(session_id: str, tool_name: str = "TaskCreate") -> dict:
    """Assistant turn containing a TaskCreate tool use."""
    return {
        "type": "assistant",
        "sessionId": session_id,
        "uuid": f"{session_id}-a-task",
        "timestamp": "2025-05-01T20:01:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": tool_name, "id": "tc1", "input": {}}],
        },
    }


def _interrupt(session_id: str) -> dict:
    """queue-operation interrupt record."""
    return {
        "type": "queue-operation",
        "sessionId": session_id,
        "uuid": f"{session_id}-qop",
        "timestamp": "2025-05-01T20:05:00.000Z",
        "operation": "enqueue",
        "content": "stop",
    }


def _write_session(tmp_path: Path, session_id: str, records: list[dict]) -> Path:
    """Write records to a JSONL file under tmp_path and return the path."""
    f = tmp_path / f"{session_id}.jsonl"
    with f.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        fh.write("\n")  # blank line — must be tolerated
    return f


def _action_bias_corpus(tmp_path: Path) -> list[Path]:
    """Three-session corpus shaped for a high-autonomy, fan-out heavy operator."""

    # Session 1: fan-out + autonomy + interrupts + waves planning + late-evening hours
    s1 = "sess-0001"
    files = []
    files.append(
        _write_session(
            tmp_path,
            s1,
            [
                _user(s1, "fan out these tasks across 10 agents", "2025-05-01T22:00:00.000Z"),
                _user(s1, "ok", "2025-05-01T22:01:00.000Z"),
                _user(s1, "do it in waves", "2025-05-01T22:02:00.000Z"),
                _user(s1, "yes", "2025-05-01T22:03:00.000Z"),
                _user(s1, "spin up 5 workers in parallel", "2025-05-01T22:04:00.000Z"),
                _user(s1, "go", "2025-05-01T22:05:00.000Z"),
                _interrupt(s1),
                _interrupt(s1),
                _assistant_task(s1),
            ],
        )
    )

    # Session 2: more fan-out + autonomy + late-evening
    s2 = "sess-0002"
    files.append(
        _write_session(
            tmp_path,
            s2,
            [
                _user(s2, "use 8 agents to fan out the work", "2025-05-02T21:00:00.000Z"),
                _user(s2, "continue", "2025-05-02T21:01:00.000Z"),
                _user(s2, "run them in parallel", "2025-05-02T21:02:00.000Z"),
                _user(s2, "sure", "2025-05-02T21:03:00.000Z"),
                _user(s2, "run it in waves please", "2025-05-02T21:04:00.000Z"),
                _assistant_task(s2),
            ],
        )
    )

    # Session 3: short ops-burst session, no fan-out, no subagent
    s3 = "sess-0003"
    files.append(
        _write_session(
            tmp_path,
            s3,
            [
                _user(s3, "deploy the server", "2025-05-03T21:30:00.000Z"),
                _user(s3, "yes", "2025-05-03T21:31:00.000Z"),
                _user(s3, "ok", "2025-05-03T21:32:00.000Z"),
            ],
        )
    )

    return files


# ---------------------------------------------------------------------------
# Tests: extract_workflow
# ---------------------------------------------------------------------------


class TestExtractWorkflowEmpty:
    def test_empty_list_returns_defaults(self):
        p = extract_workflow([])
        assert p.sample_size == 0
        assert p.fan_out_hits_per_session == 0.0
        assert p.fan_out_vocabulary == []
        assert p.typical_agent_count == 0
        assert p.autonomy_score == 0.0
        assert p.interrupt_rate == 0.0
        assert p.planning_idiom == ""
        assert p.peak_hours == []
        assert p.preferred_work_window == "mixed"
        assert p.session_shape == "mixed"
        assert p.subagent_adoption == 0.0

    def test_returns_workflowprofile_instance(self):
        p = extract_workflow([])
        assert isinstance(p, WorkflowProfile)


class TestExtractWorkflowActionBias:
    @pytest.fixture
    def profile(self, tmp_path):
        files = _action_bias_corpus(tmp_path)
        return extract_workflow(files)

    def test_sample_size(self, profile):
        assert profile.sample_size == 3

    def test_fan_out_hits_nonzero(self, profile):
        # Sessions 1 and 2 both have fan-out requests.
        assert profile.fan_out_hits_per_session > 0.0

    def test_fan_out_vocabulary_populated(self, profile):
        assert len(profile.fan_out_vocabulary) > 0
        # At least one fan-out phrase should be in the vocab.
        known_phrases = {"fan out", "spin up", "in parallel", "use .* agents", "fan-out"}
        assert any(p in known_phrases for p in profile.fan_out_vocabulary)

    def test_typical_agent_count_positive(self, profile):
        # "10 agents" and "8 agents" → median ~= 9
        assert profile.typical_agent_count > 0

    def test_autonomy_score_high(self, profile):
        # Many one-word turns: yes, ok, go, continue, sure
        assert profile.autonomy_score > 0.3

    def test_interrupt_rate_nonzero(self, profile):
        # Session 1 has 2 interrupts; sessions 2 and 3 have 0 → avg = 2/3
        assert profile.interrupt_rate > 0.0

    def test_planning_idiom_waves(self, profile):
        assert profile.planning_idiom == "waves"

    def test_peak_hours_evening(self, profile):
        # All messages are in 21:00–22:xx UTC range.
        assert len(profile.peak_hours) > 0
        assert all(isinstance(h, int) for h in profile.peak_hours)
        # At least one of the top hours should be in the 20–22 range.
        assert any(20 <= h <= 22 for h in profile.peak_hours)

    def test_preferred_work_window_evening_or_late_night(self, profile):
        # 20:00–22:xx is in the evening/late_night bins.
        assert profile.preferred_work_window in ("evening", "late_night", "mixed")

    def test_session_shape_ops_burst_or_bimodal(self, profile):
        # Two sessions have ≤6 user turns (ops_burst territory).
        assert profile.session_shape in ("ops_burst", "bimodal", "mixed")

    def test_subagent_adoption_partial(self, profile):
        # Sessions 1 and 2 have TaskCreate; session 3 does not → 2/3 ≈ 0.67
        assert 0.5 <= profile.subagent_adoption <= 1.0

    def test_autonomy_score_bounded(self, profile):
        assert 0.0 <= profile.autonomy_score <= 1.0

    def test_subagent_adoption_bounded(self, profile):
        assert 0.0 <= profile.subagent_adoption <= 1.0


class TestExtractWorkflowMalformedInput:
    def test_skips_bad_json_lines(self, tmp_path):
        f = tmp_path / "bad.jsonl"
        f.write_text(
            "not json at all\n"
            '{"type": "user", "sessionId": "x", '
            '"message": {"role": "user", "content": "ok"}}\n'
            "bad again\n"
        )
        # Should not raise; should return a valid profile.
        p = extract_workflow([f])
        assert isinstance(p, WorkflowProfile)
        assert p.sample_size >= 0

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "blank.jsonl"
        f.write_text("\n\n\n")
        p = extract_workflow([f])
        assert isinstance(p, WorkflowProfile)


# ---------------------------------------------------------------------------
# Tests: extract_workflow_incremental
# ---------------------------------------------------------------------------


def _corpus_records(tmp_path: Path) -> list[dict]:
    """Return all raw record dicts from the action-bias corpus — same shape
    that ``_profile_record_snapshot`` emits for the hot path in ingest.py."""
    files = _action_bias_corpus(tmp_path)
    records: list[dict] = []
    for f in files:
        for line in f.read_text().splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    records.append(json.loads(line))
    return records


class TestExtractWorkflowIncremental:
    @pytest.fixture
    def batch_profile(self, tmp_path):
        files = _action_bias_corpus(tmp_path)
        return extract_workflow(files)

    # ------------------------------------------------------------------
    # test_incremental_from_records_and_dict — replicates the ACTUAL ingest
    # wiring types that were broken before this fix.  Builds a list of raw
    # record dicts + an existing DICT exactly like get_workflow() returns
    # (with _updated_ts sidecar), then calls extract_workflow_incremental
    # with that pairing.  This is the test that would have caught the bug.
    # ------------------------------------------------------------------

    def test_incremental_from_records_and_dict(self, tmp_path):
        """Replicate the exact types ingest.py passes: records + dict with _updated_ts."""
        records = _corpus_records(tmp_path)
        # Simulate what index.workflow.get_workflow() returns after _updated_ts.pop()
        # is NOT called (we want to prove _normalize_existing handles it).
        existing_dict = {
            "fan_out_hits_per_session": 1.0,
            "fan_out_vocabulary": ["fan out"],
            "typical_agent_count": 5,
            "autonomy_score": 0.4,
            "interrupt_rate": 0.5,
            "planning_idiom": "steps",
            "peak_hours": [14, 15, 16],
            "preferred_work_window": "afternoon",
            "session_shape": "focused",
            "subagent_adoption": 0.3,
            "sample_size": 20,
            # The sidecar key that caused 'dict object has no attribute sample_size'
            "_updated_ts": {"autonomy_score": 1716000000, "sample_size": 1716000000},
        }
        # Must not raise; must return a valid profile with sample_size > 0.
        result = extract_workflow_incremental(records, existing_dict)
        assert isinstance(result, WorkflowProfile)
        assert result.sample_size > 0, "sample_size must be > 0 for a non-empty record batch"

    def test_incremental_existing_none_cold_start(self, tmp_path):
        """existing=None (cold start) should return the batch profile directly."""
        records = _corpus_records(tmp_path)
        result = extract_workflow_incremental(records, existing=None)
        assert isinstance(result, WorkflowProfile)
        assert result.sample_size > 0

    def test_incremental_existing_workflowprofile_backcompat(self, tmp_path):
        """existing as a WorkflowProfile object must still work (back-compat)."""
        records = _corpus_records(tmp_path)
        existing_wp = WorkflowProfile(autonomy_score=0.0, sample_size=10)
        result = extract_workflow_incremental(records, existing=existing_wp)
        assert isinstance(result, WorkflowProfile)
        assert result.sample_size > 0
        # Should have blended toward the batch's non-zero autonomy.
        assert result.autonomy_score > 0.0

    def test_cold_start_records_match_full_corpus(self, tmp_path):
        """Records from the same corpus should yield same profile as extract_workflow."""
        files = _action_bias_corpus(tmp_path)
        p_full = extract_workflow(files)
        records = _corpus_records(tmp_path)
        p_inc = extract_workflow_incremental(records, existing=None)
        assert p_full.autonomy_score == p_inc.autonomy_score
        assert p_full.fan_out_hits_per_session == p_inc.fan_out_hits_per_session
        assert p_full.sample_size == p_inc.sample_size

    def test_no_new_records_returns_existing(self):
        existing = WorkflowProfile(autonomy_score=0.8, sample_size=10)
        result = extract_workflow_incremental([], existing=existing)
        assert result.autonomy_score == 0.8
        assert result.sample_size == 10

    def test_ema_blends_toward_batch(self, tmp_path):
        records = _corpus_records(tmp_path)
        # Existing has autonomy 0.0; the batch has high autonomy.
        existing = WorkflowProfile(autonomy_score=0.0, sample_size=50)
        result = extract_workflow_incremental(records, existing=existing, window_size=50)
        # With batch_n=3 and window=50, alpha=3/50=0.06; new value should be
        # slightly above 0.0 but much less than the batch's raw value.
        assert result.autonomy_score > 0.0

    def test_large_batch_dominates(self, tmp_path):
        records = _corpus_records(tmp_path)
        # Existing has low autonomy, window = 3 (same as batch size) → alpha=1.0
        existing = WorkflowProfile(autonomy_score=0.0, sample_size=1)
        result = extract_workflow_incremental(records, existing=existing, window_size=3)
        batch = extract_workflow(_action_bias_corpus(tmp_path))
        # alpha=1.0 → result should equal batch values exactly.
        assert result.autonomy_score == batch.autonomy_score

    def test_sample_size_clamped_to_window(self, tmp_path):
        records = _corpus_records(tmp_path)
        existing = WorkflowProfile(sample_size=48)
        result = extract_workflow_incremental(records, existing=existing, window_size=50)
        assert result.sample_size <= 50


# ---------------------------------------------------------------------------
# Tests: SQLite storage round-trip
# ---------------------------------------------------------------------------


class TestPersistWorkflow:
    @pytest.fixture
    def mem_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.close()

    def test_ensure_schema_creates_table(self, mem_conn):
        ensure_schema(mem_conn)
        row = mem_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_profile'"
        ).fetchone()
        assert row is not None

    def test_ensure_schema_idempotent(self, mem_conn):
        ensure_schema(mem_conn)
        ensure_schema(mem_conn)  # second call must not raise

    def test_persist_and_get_round_trip(self, mem_conn):
        profile = WorkflowProfile(
            fan_out_hits_per_session=2.5,
            fan_out_vocabulary=["fan out", "spin up"],
            typical_agent_count=8,
            autonomy_score=0.75,
            interrupt_rate=3.2,
            planning_idiom="waves",
            peak_hours=[21, 22, 20],
            preferred_work_window="evening",
            session_shape="ops_burst",
            subagent_adoption=0.6,
            sample_size=50,
        )
        persist_workflow(mem_conn, profile)
        mem_conn.commit()

        result = get_workflow(mem_conn)

        assert result["fan_out_hits_per_session"] == pytest.approx(2.5)
        assert result["fan_out_vocabulary"] == ["fan out", "spin up"]
        assert result["typical_agent_count"] == 8
        assert result["autonomy_score"] == pytest.approx(0.75)
        assert result["planning_idiom"] == "waves"
        assert result["peak_hours"] == [21, 22, 20]
        assert result["preferred_work_window"] == "evening"
        assert result["session_shape"] == "ops_burst"
        assert result["subagent_adoption"] == pytest.approx(0.6)
        assert result["sample_size"] == 50

    def test_get_empty_table_returns_sidecar_only(self, mem_conn):
        ensure_schema(mem_conn)
        result = get_workflow(mem_conn)
        assert set(result.keys()) == {"_updated_ts"}
        assert result["_updated_ts"] == {}

    def test_updated_ts_sidecar_present(self, mem_conn):
        profile = WorkflowProfile(sample_size=1, autonomy_score=0.5)
        persist_workflow(mem_conn, profile, updated_ts=9999)
        mem_conn.commit()
        result = get_workflow(mem_conn)
        assert "_updated_ts" in result
        assert result["_updated_ts"].get("autonomy_score") == 9999

    def test_upsert_overwrites_previous(self, mem_conn):
        p1 = WorkflowProfile(autonomy_score=0.1, sample_size=5)
        persist_workflow(mem_conn, p1)
        mem_conn.commit()
        p2 = WorkflowProfile(autonomy_score=0.9, sample_size=10)
        persist_workflow(mem_conn, p2)
        mem_conn.commit()
        result = get_workflow(mem_conn)
        assert result["autonomy_score"] == pytest.approx(0.9)
        assert result["sample_size"] == 10

    def test_lists_round_trip(self, mem_conn):
        profile = WorkflowProfile(
            peak_hours=[0, 1, 2],
            fan_out_vocabulary=["a phrase", "another phrase"],
            sample_size=1,
        )
        persist_workflow(mem_conn, profile)
        mem_conn.commit()
        result = get_workflow(mem_conn)
        assert result["peak_hours"] == [0, 1, 2]
        assert result["fan_out_vocabulary"] == ["a phrase", "another phrase"]


# ---------------------------------------------------------------------------
# Smoke test: imports
# ---------------------------------------------------------------------------


def test_imports():
    from extractors import workflow as w_extractor
    from index import workflow as w_index
    from mcp_server.extras import workflow_tools as w_tools

    assert hasattr(w_extractor, "extract_workflow")
    assert hasattr(w_extractor, "extract_workflow_from_records")
    assert hasattr(w_index, "persist_workflow")
    assert hasattr(w_tools, "get_workflow_profile")


# ---------------------------------------------------------------------------
# Tests: extract_workflow_from_records (public record-stream entry point)
# ---------------------------------------------------------------------------


class TestExtractWorkflowFromRecords:
    """Verify the public record-stream API handles raw dicts, Record objects,
    and mixed iterables without error."""

    def test_empty_iterable_returns_defaults(self):
        from extractors.workflow import extract_workflow_from_records

        p = extract_workflow_from_records([])
        assert isinstance(p, WorkflowProfile)
        assert p.sample_size == 0

    def test_raw_dicts_produce_same_result_as_extract_workflow(self, tmp_path):
        from extractors.workflow import extract_workflow_from_records

        files = _action_bias_corpus(tmp_path)
        records = []
        for f in files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    with contextlib.suppress(json.JSONDecodeError):
                        records.append(json.loads(line))
        p_files = extract_workflow(files)
        p_recs = extract_workflow_from_records(records)
        assert p_recs.sample_size == p_files.sample_size
        assert p_recs.fan_out_hits_per_session == p_files.fan_out_hits_per_session
        assert p_recs.autonomy_score == p_files.autonomy_score

    def test_record_objects_unwrapped_via_raw(self):
        """Record objects carrying .raw dicts are correctly unwrapped."""

        from extractors.workflow import extract_workflow_from_records

        class FakeRecord:
            def __init__(self, raw):
                self.raw = raw

        raw1 = {
            "type": "user",
            "sessionId": "sR1",
            "timestamp": "2025-05-01T22:00:00.000Z",
            "message": {"role": "user", "content": "fan out these tasks"},
        }
        raw2 = {
            "type": "user",
            "sessionId": "sR1",
            "timestamp": "2025-05-01T22:01:00.000Z",
            "message": {"role": "user", "content": "ok"},
        }

        records = [FakeRecord(raw1), FakeRecord(raw2)]
        p = extract_workflow_from_records(records)
        assert isinstance(p, WorkflowProfile)
        assert p.sample_size == 1
        assert p.fan_out_hits_per_session > 0

    def test_mixed_dicts_and_records_accepted(self):
        """Mix of raw dicts and Record-like objects processes without error."""
        from extractors.workflow import extract_workflow_from_records

        class FakeRec:
            def __init__(self, raw):
                self.raw = raw

        raw_dict = {
            "type": "user",
            "sessionId": "sm1",
            "timestamp": "2025-05-01T20:00:00.000Z",
            "message": {"role": "user", "content": "spin up workers"},
        }
        raw_rec = FakeRec(
            {
                "type": "user",
                "sessionId": "sm2",
                "timestamp": "2025-05-01T20:00:00.000Z",
                "message": {"role": "user", "content": "go"},
            }
        )

        p = extract_workflow_from_records([raw_dict, raw_rec])
        assert isinstance(p, WorkflowProfile)
        assert p.sample_size >= 1
