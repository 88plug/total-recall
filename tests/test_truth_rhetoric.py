"""Tests for the 7-category truth-assertion extractor.

One positive case per category + two near-miss negative cases (text that
shares vocabulary with the patterns but should *not* fire).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from extractors.truth_rhetoric import CATEGORIES, TruthRhetoric

# ---------------------------------------------------------------------------
# Minimal RecordLike fixture (mirrors tests/test_extractors.py to keep this
# file self-contained — see that module for the master copy).
# ---------------------------------------------------------------------------


@dataclass
class FakeRecord:
    type: str
    uuid: str
    parent_uuid: str | None = None
    session_id: str = "sess-rhet"
    cwd: str = "/home/operator/proj"
    ts: datetime = field(
        default_factory=lambda: datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    )
    role: str | None = None
    content_kind: str | None = None
    content: Any = None
    text: str | None = None
    is_meta: bool = False
    is_compact_summary: bool = False
    is_sidechain: bool = False
    subtype: str | None = None
    payload: dict | None = None


def _user(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="user",
        uuid=kw.pop("uuid", f"u-{abs(hash(text)) % 10_000}"),
        role="user",
        content_kind="string",
        text=text,
        content=text,
        **kw,
    )


def _assistant(text: str, **kw: Any) -> FakeRecord:
    return FakeRecord(
        type="assistant",
        uuid=kw.pop("uuid", f"a-{abs(hash(text)) % 10_000}"),
        role="assistant",
        content_kind="blocks",
        text=text,
        content=[{"type": "text", "text": text}],
        **kw,
    )


class FakeDag:
    def __init__(self, records: list[FakeRecord]) -> None:
        self._records = records
        self._idx = {r.uuid: i for i, r in enumerate(records)}

    def get(self, uuid: str) -> FakeRecord | None:
        for r in self._records:
            if r.uuid == uuid:
                return r
        return None

    def parent_of(self, uuid: str) -> FakeRecord | None:
        rec = self.get(uuid)
        if rec is None or rec.parent_uuid is None:
            return None
        return self.get(rec.parent_uuid)

    def prev_assistant_turn(self, uuid: str) -> FakeRecord | None:
        i = self._idx.get(uuid)
        if i is None:
            return None
        for j in range(i - 1, -1, -1):
            if self._records[j].type == "assistant":
                return self._records[j]
        return None

    def next_user_turn(self, uuid: str, within: int = 5) -> FakeRecord | None:
        i = self._idx.get(uuid)
        if i is None:
            return None
        for j in range(i + 1, min(len(self._records), i + 1 + within)):
            if self._records[j].type == "user":
                return self._records[j]
        return None


# ---------------------------------------------------------------------------
# Per-category positive cases — one apiece, 7 total.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_category",
    [
        ("no, i said use port 8443, not 443", "restatement"),
        ("you told me earlier we use provider-y, not provider-x", "quote_back"),
        ("never use stripe in this project", "standing_rule"),
        ("check our session logs for the previous decision", "past_logs_appeal"),
        ("you are drifting again, stop", "drift_callout"),
        ("are you stupid? i already explained this", "capability_insult"),
        ("you verify it works before claiming done", "verify_yourself_push"),
    ],
)
def test_truth_rhetoric_positive_one_per_category(text, expected_category):
    a_prev = _assistant("Some prior model output that triggered the pushback.", uuid="a-prev")
    u = _user(text, uuid="u-target", parent_uuid="a-prev")
    records = [a_prev, u]
    dag = FakeDag(records)

    results = list(TruthRhetoric().extract(records, dag=dag))
    assert len(results) == 1, f"expected exactly one hit for {text!r}, got {results}"
    ext = results[0]
    assert ext.kind == "truth_assertion"
    assert ext.context["category"] == expected_category
    assert ext.context["preceding_assistant_uuid"] == "a-prev"
    assert ext.context["preceding_assistant_text_excerpt"] is not None
    assert "Some prior model output" in ext.context["preceding_assistant_text_excerpt"]
    assert 0.0 < ext.context["severity"] <= 1.0
    # capability_insult gets the +0.15 bump.
    if expected_category == "capability_insult":
        # base 0.5 + insult-word 0.15 + restatement-pattern "already" 0.1 +
        # short-text 0.1 + category bump 0.15 = 1.0 (clamped).
        assert ext.context["severity"] >= 0.85


def test_truth_rhetoric_all_seven_categories_covered():
    """Sanity check: the parametrize list above exercises every category."""
    covered = {
        "restatement",
        "quote_back",
        "standing_rule",
        "past_logs_appeal",
        "drift_callout",
        "capability_insult",
        "verify_yourself_push",
    }
    assert covered == set(CATEGORIES)


# ---------------------------------------------------------------------------
# Negative cases — text that *looks* like a match but isn't.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # "no" + "said" in the same sentence but not the restatement shape,
        # and no other category fires.
        "no problem, i think we said yes to that proposal last week",
        # Mentions "session logs" but is not an appeal to check them.
        "the session logs format changed in the v2 release notes",
    ],
)
def test_truth_rhetoric_negative_cases(text):
    rec = _user(text)
    results = list(TruthRhetoric().extract([rec]))
    assert results == [], f"should not match: {text!r}, got {results}"
