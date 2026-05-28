"""Real-corpus backtest — local-only, PII-bearing.

Runs the data-driven discovery extractors against the operator's actual
~/.claude/projects/*.jsonl corpus and asserts they rediscover the operator
*from the data* — proof the retool works on real data, not just the
synthetic Dana Lopez fixture in tests/test_generic_operator.py.

PRIVACY: this whole directory (tests/local/) is .gitignored. The ground-truth
JSON loaded from ``BT_GROUND_TRUTH`` (default ``tests/local/ground_truth.json``)
contains real values and must never be committed. Assertion failure messages
do not embed PII — they report shape/cardinality only.

Run:
    BT_GROUND_TRUTH=tests/local/ground_truth.json \\
        uv run python -m pytest tests/local/test_backtest_real_corpus.py -v

The test module is skipped automatically if either the corpus or the
ground-truth file is absent.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

CORPUS_ROOT = Path(
    os.environ.get("TOTAL_RECALL_CORPUS_ROOT", str(Path.home() / ".claude" / "projects"))
).expanduser()
GT_PATH = Path(os.environ.get("BT_GROUND_TRUTH", "tests/local/ground_truth.json"))

GT: dict = (
    json.loads(GT_PATH.read_text()) if GT_PATH.exists() and GT_PATH.is_file() else {}
)

pytestmark = pytest.mark.skipif(
    not CORPUS_ROOT.exists() or not GT,
    reason="real corpus or ground_truth.json absent — local-only backtest",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _all_jsonl_paths() -> list[Path]:
    return sorted(CORPUS_ROOT.glob("*/*.jsonl"))


def _iter_records_for_voice(paths: list[Path]):
    """Yield raw JSONL dicts so measure_voice can filter for user-string turns."""
    for p in paths:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# --------------------------------------------------------------------------- #
# Layer 1 — operator_profile
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def profile():
    from extractors.operator_profile import extract_operator_profile
    t0 = time.time()
    p = extract_operator_profile(_all_jsonl_paths())
    elapsed = time.time() - t0
    print(f"\n[operator_profile] extraction time: {elapsed:.1f}s")
    return p


def test_profile_name_rediscovered(profile):
    candidates = set(GT["profile"]["name_candidates"])
    assert profile.name, "operator name is empty — data-driven extractor produced no signal"
    assert profile.name in candidates, (
        f"name not in top-{len(candidates)} ground-truth candidates"
    )


def test_profile_email_rediscovered(profile):
    candidates = set(GT["profile"]["email_candidates"])
    assert profile.email_primary, "email_primary is empty"
    assert profile.email_primary in candidates, (
        f"email_primary not in top-{len(candidates)} ground-truth candidates"
    )


def test_profile_handle_rediscovered(profile):
    candidates = {h.lower() for h in GT["profile"]["github_handles"]}
    found = (profile.handle or profile.github_user or "").lower()
    assert found, "handle/github_user both empty"
    assert found in candidates, (
        f"handle/github_user not in top-{len(candidates)} ground-truth candidates"
    )


def test_profile_machines_overlap(profile):
    needles = [s.lower() for s in GT["profile"]["machine_substrings"]]
    found_hosts = " ".join(profile.machines.keys()).lower() if profile.machines else ""
    hits = [n for n in needles if n in found_hosts]
    assert hits, (
        f"machines empty or no ground-truth substring match "
        f"(needles={len(needles)}, found_hosts_count={len(profile.machines)})"
    )


def test_profile_billing_rediscovered(profile):
    if not profile.billing_rail:
        pytest.skip("billing_rail empty — no signal in profile (acceptable)")
    rails = {r.lower() for r in GT["profile"]["billing_rails"]}
    assert profile.billing_rail.lower() in rails, (
        f"billing_rail not in ground-truth set (size={len(rails)})"
    )


def test_profile_no_password_field(profile):
    assert not hasattr(profile, "default_root_pw"), (
        "REGRESSION: default_root_pw resurrected on OperatorProfile"
    )
    blob = profile.to_field_dict()
    assert "default_root_pw" not in blob, "default_root_pw key present in to_field_dict()"


# --------------------------------------------------------------------------- #
# Layer 2 — ontology / vocabulary miner
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def discovered_vocab():
    from extractors.ontology import mine_vocabulary
    t0 = time.time()
    terms = mine_vocabulary(CORPUS_ROOT, min_sessions=2, min_freq=4)
    elapsed = time.time() - t0
    discovered = {t.term.lower() for t in terms}
    print(
        f"\n[vocab] mining time: {elapsed:.1f}s, "
        f"discovered={len(discovered)} terms (min_sessions=2, min_freq=4)"
    )
    return discovered


def test_vocab_recall_meets_threshold(discovered_vocab):
    top = {t.lower() for t in GT["vocab"]["top_terms"]}
    hits = top & discovered_vocab
    recall = len(hits) / len(top) if top else 0.0
    threshold = float(GT["vocab"].get("recall_threshold", 0.40))
    print(f"[vocab] recall={recall:.0%} ({len(hits)}/{len(top)}) — threshold {threshold:.0%}")
    assert recall >= threshold, (
        f"vocabulary recall {recall:.0%} < threshold {threshold:.0%} "
        f"({len(hits)}/{len(top)} ground-truth terms surfaced)"
    )


def test_vocab_no_stopword_leakage(discovered_vocab):
    stopwords = {w.lower() for w in GT["vocab"]["stopwords_sample"]}
    leaks = stopwords & discovered_vocab
    assert not leaks, f"{len(leaks)} stopwords surfaced in vocabulary (sample size {len(stopwords)})"


# --------------------------------------------------------------------------- #
# Layer 3 — voice profile
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def voice():
    from extractors.voice_profile import measure_voice
    t0 = time.time()
    v = measure_voice(_iter_records_for_voice(_all_jsonl_paths()))
    elapsed = time.time() - t0
    print(
        f"\n[voice] measure time: {elapsed:.1f}s, "
        f"sample_size={v['sample_size']}, "
        f"lc_start={v['lowercase_start_pct']:.2f}, "
        f"one_word={v['one_word_turn_pct']:.2f}, "
        f"we_vs_i={v['we_vs_i_ratio']:.2f}, "
        f"typos={len(v['signature_typos'])}"
    )
    return v


def test_voice_sample_size_sufficient(voice):
    min_n = int(GT["voice"]["min_sample_size"])
    assert voice["sample_size"] >= min_n, (
        f"sample_size {voice['sample_size']} below min {min_n}"
    )


def test_voice_lowercase_in_range(voice):
    lo, hi = GT["voice"]["lowercase_start_pct_range"]
    v = voice["lowercase_start_pct"]
    assert lo <= v <= hi, f"lowercase_start_pct {v:.2f} out of [{lo}, {hi}]"


def test_voice_we_vs_i_in_range(voice):
    lo, hi = GT["voice"]["we_vs_i_range"]
    v = voice["we_vs_i_ratio"]
    assert lo <= v <= hi, f"we_vs_i_ratio {v:.2f} out of [{lo}, {hi}]"


def test_voice_one_word_in_range(voice):
    lo, hi = GT["voice"]["one_word_pct_range"]
    v = voice["one_word_turn_pct"]
    assert lo <= v <= hi, f"one_word_turn_pct {v:.2f} out of [{lo}, {hi}]"


def test_voice_learned_typos_overlap_ground_truth(voice):
    expected = {t.lower() for t in GT["voice"]["typo_candidates"]}
    learned = {t.lower() for t, _ in voice["signature_typos"]}
    overlap = expected & learned
    min_overlap = int(GT["voice"].get("min_typo_overlap", 1))
    print(
        f"[voice] typo overlap: {len(overlap)} of {len(expected)} expected "
        f"(learned set size: {len(learned)})"
    )
    assert len(overlap) >= min_overlap, (
        f"typo overlap {len(overlap)} < min {min_overlap} "
        f"(expected set size {len(expected)}, learned set size {len(learned)})"
    )


# --------------------------------------------------------------------------- #
# Layer 4 — scope_detect (no-DB basename fallback)
# --------------------------------------------------------------------------- #

def test_scope_detect_resolves_real_cwds():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hooks" / "lib"))
    import scope_detect  # noqa: E402

    mapping = GT["scopes"]["cwd_to_substring"]
    misses: list[str] = []
    for cwd, substr in mapping.items():
        result = (scope_detect.infer_scope(cwd) or "").lower()
        if substr.lower() not in result:
            misses.append(cwd)
    assert not misses, (
        f"{len(misses)}/{len(mapping)} cwds failed scope-substring check"
    )


# --------------------------------------------------------------------------- #
# Layer 5 — incremental path parity (regression guard for path divergence)
# --------------------------------------------------------------------------- #

# Valid IANA region prefixes that any extracted timezone must start with.
# Bare "UTC" is also canonical. Anything else (e.g. "Jc/Jmin/Jmax") is
# garbage produced by a broken regex that was not anchored to IANA regions.
_IANA_PREFIX_RE = re.compile(
    r"^(Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia"
    r"|Europe|Indian|Pacific|US|Etc|GMT)/|^UTC$"
)


def _valid_iana_or_empty(tz: str) -> bool:
    """Return True when ``tz`` is empty or matches a valid IANA shape."""
    if not tz:
        return True
    return bool(_IANA_PREFIX_RE.match(tz))


def _stream_records(paths: list[Path]):
    """Yield raw JSONL dicts from a list of JSONL paths."""
    for p in paths:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


@pytest.fixture(scope="module")
def incremental_profile():
    """Run ``extract_incremental`` over the full real corpus and return the
    merged profile.  This exercises the Stop-hook code path that differs from
    ``extract_operator_profile`` and was the source of the timezone-garbage
    and wrong-handle bugs.
    """
    from extractors.operator_profile import extract_incremental, OperatorProfile

    paths = _all_jsonl_paths()
    t0 = time.time()
    merged = extract_incremental(list(_stream_records(paths)), existing_profile=None)
    elapsed = time.time() - t0
    print(
        f"\n[incremental_profile] extraction time: {elapsed:.1f}s, "
        f"timezone={merged.timezone!r}, handle={merged.handle!r}"
    )
    return merged


def test_incremental_timezone_not_garbage(incremental_profile):
    """The incremental path must never emit a garbage timezone like
    ``Jc/Jmin/Jmax``.  The resolved timezone must either be empty or
    match a valid IANA ``Region/City`` shape.
    """
    tz = incremental_profile.timezone
    assert _valid_iana_or_empty(tz), (
        f"Incremental path produced invalid timezone: {tz!r}. "
        "Only valid IANA zones (e.g. America/New_York, Europe/Berlin) "
        "or empty string are acceptable."
    )


def test_incremental_handle_in_ground_truth(incremental_profile):
    """The incremental path must resolve a handle in the ground-truth
    ``github_handles`` candidate set — it must not surface an unrelated
    high-frequency token from the corpus.
    """
    candidates = {h.lower() for h in GT["profile"]["github_handles"]}
    resolved = (
        incremental_profile.handle or incremental_profile.github_user or ""
    ).lower()

    assert resolved, (
        "incremental path: handle/github_user both empty — "
        "operator identity not recovered via incremental extractor"
    )
    assert resolved in candidates, (
        f"incremental path: handle/github_user not in "
        f"top-{len(candidates)} ground-truth candidates "
        f"(got handle count: 1, expected one of {len(candidates)} candidates)"
    )


@pytest.fixture(scope="module")
def incremental_persisted_profile(tmp_path_factory):
    """Ingest the real corpus into a temp SQLite DB via the full
    ingest+persist pipeline (the path the Stop hook actually takes),
    then read back the ``operator_profile`` table rows.

    This validates the end-to-end persisted path, not just the in-memory
    extraction result.
    """
    from extractors.operator_profile import (
        extract_incremental,
        persist_incremental_profile,
        OperatorProfile,
    )
    from index.operator import OPERATOR_PROFILE_SCHEMA, ensure_schema, get_profile

    tmp = tmp_path_factory.mktemp("incr_db")
    db_path = tmp / "test_incr.db"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(OPERATOR_PROFILE_SCHEMA)
    ensure_schema(conn)

    paths = _all_jsonl_paths()
    t0 = time.time()
    merged = extract_incremental(
        list(_stream_records(paths)), existing_profile=None
    )
    persist_incremental_profile(conn, merged)
    conn.commit()
    elapsed = time.time() - t0

    stored = get_profile(conn)
    conn.close()
    print(
        f"\n[incremental_persisted] ingest+persist time: {elapsed:.1f}s, "
        f"timezone={stored.get('timezone', '')!r}, "
        f"handle={stored.get('handle', '')!r}"
    )
    return stored


def test_persisted_incremental_timezone_not_garbage(incremental_persisted_profile):
    """The persisted incremental profile must not contain a garbage timezone."""
    tz = incremental_persisted_profile.get("timezone", "")
    assert _valid_iana_or_empty(tz), (
        f"Persisted incremental profile has invalid timezone: {tz!r}. "
        "Check that extract_incremental uses the same anchored IANA regex "
        "as extract_operator_profile."
    )


def test_persisted_incremental_handle_in_ground_truth(incremental_persisted_profile):
    """The persisted incremental profile must carry a ground-truth handle."""
    candidates = {h.lower() for h in GT["profile"]["github_handles"]}
    resolved = (
        incremental_persisted_profile.get("handle", "")
        or incremental_persisted_profile.get("github_user", "")
        or ""
    ).lower()

    assert resolved, (
        "persisted incremental profile: handle/github_user both missing or empty"
    )
    assert resolved in candidates, (
        f"persisted incremental profile: handle not in "
        f"top-{len(candidates)} ground-truth candidates "
        f"(got handle count: 1, expected one of {len(candidates)} candidates)"
    )
