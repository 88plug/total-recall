"""Regression tests for signature-typo detection.

Three defects made this field report the operator's ordinary vocabulary as
their personal typos. On a real 3.3M-char corpus it led with:

    node 4378, per 2170, crashed 2168, running 1250, gaudi 1237

1. Only ``/usr/share/dict/words`` was probed. That path does not exist on
   Arch/Manjaro (which ship ``dict/cracklib-small``), so those hosts fell
   back to an embedded list of 239 words — the docstring claimed ~600 — and
   every normal word passed the "not English" test.
2. Nothing marked the degraded state, so a 239-word list was used as though
   it were a dictionary.
3. Absence from the dictionary was treated as sufficient. It is not:
   ``gaudi``, ``nixl`` and ``vllm`` are vocabulary, not misspellings.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extractors import voice_profile as vp  # noqa: E402

# ---------------------------------------------------------------------------
# Wordlist loading
# ---------------------------------------------------------------------------


def test_wordlist_probes_more_than_one_path() -> None:
    """Arch/Manjaro ship cracklib-small, not words; both must be tried."""
    paths = vp._SYSTEM_WORDLIST_PATHS
    assert "/usr/share/dict/words" in paths
    assert any("cracklib" in p for p in paths)
    assert any(p.endswith(".dic") for p in paths), "hunspell/myspell"


def test_hunspell_entries_drop_affix_flags() -> None:
    """``running/AG`` must normalise to ``running`` or it never matches."""
    assert vp._normalize_wordlist_entry("running/AG\n") == "running"
    assert vp._normalize_wordlist_entry("Cache\n") == "cache"
    assert vp._normalize_wordlist_entry("  spaced  ") == "spaced"
    assert vp._normalize_wordlist_entry("49284\n") == "", "hunspell count line"
    assert vp._normalize_wordlist_entry("has-hyphen") == ""
    assert vp._normalize_wordlist_entry("") == ""


def test_loaded_wordlist_knows_ordinary_base_words() -> None:
    """A real dictionary carries everyday lemmas.

    Base forms only. Which *inflections* a wordlist carries varies — a lemma
    dictionary lists ``crash`` and leaves ``crashed`` to affix rules, while
    cracklib-small happens to spell both out — so asserting on an inflected
    form tests the host's dictionary, not this code.
    """
    if not vp.english_wordlist_is_usable():
        import pytest

        pytest.skip("no system dictionary on this host")
    words = vp._get_english_words()
    for ordinary in ("node", "crash", "run", "fleet", "report"):
        assert ordinary in words, ordinary


def test_inflections_are_not_typos_even_when_absent_from_the_dictionary() -> None:
    """The guarantee that actually matters, and it must not need the dictionary.

    ``crashed`` and ``running`` were the seed failure. On a lemma-only
    dictionary they are still missing, so membership alone can never fix
    this — the frequency rules have to carry it. Words used thousands of
    times are vocabulary whatever the wordlist says.
    """
    lemma_only = frozenset({"crash", "run", "node", "report", "fleet", "task"})
    counts = {"crashed": 2168, "crash": 50, "running": 1250, "run": 300}
    for inflected in ("crashed", "running"):
        assert inflected not in lemma_only, "precondition: absent from the dictionary"
        assert vp._looks_like_typo(inflected, counts[inflected], lemma_only, counts) is False, (
            inflected
        )

    # ...while a real slip is still caught on the same thin dictionary.
    slips = {"task": 3204, "taks": 5}
    assert vp._looks_like_typo("taks", 5, lemma_only, slips) is True


# ---------------------------------------------------------------------------
# Typo classification
# ---------------------------------------------------------------------------


def test_domain_jargon_is_not_a_typo() -> None:
    """Absent from the dictionary, but not a misspelling of anything."""
    english = frozenset({"gaudy", "cpu", "harvest", "per", "the", "what"})
    counts = {"gaudi": 10859, "gaudy": 0, "harvestd": 773, "harvest": 2357}
    for jargon, n in (("gaudi", 10859), ("harvestd", 773)):
        assert vp._looks_like_typo(jargon, n, english, counts) is False, jargon


def test_a_real_slip_is_a_typo() -> None:
    """Far rarer than the word it misses — that is the signal."""
    english = frozenset({"the", "what", "task", "error"})
    counts = {"the": 8683, "what": 1028, "task": 3204, "error": 3047}
    for slip, n in (("thev", 10), ("waht", 5), ("taks", 5), ("rror", 5)):
        assert vp._looks_like_typo(slip, n, english, {**counts, slip: n}) is True, slip


def test_short_tokens_are_acronyms_not_typos() -> None:
    """``hpu`` sits one substitution from ``cpu``; it is not a misspelling."""
    english = frozenset({"cpu", "tap", "top"})
    counts = {"cpu": 5000, "hpu": 798, "tmp": 1837, "top": 4000}
    assert vp._looks_like_typo("hpu", 798, english, counts) is False
    assert vp._looks_like_typo("tmp", 1837, english, counts) is False


def test_token_with_no_english_neighbour_is_not_a_typo() -> None:
    english = frozenset({"cache", "server"})
    assert vp._looks_like_typo("zzqqxx", 9, english, {"zzqqxx": 9}) is False


def test_english_neighbours_covers_all_four_edits() -> None:
    english = frozenset({"the", "task", "cache"})
    assert "the" in vp._english_neighbours("thex", english), "substitution"
    assert "the" in vp._english_neighbours("teh", english), "transposition"
    assert "the" in vp._english_neighbours("thre", english), "deletion"
    assert "the" in vp._english_neighbours("th", english), "insertion"
    assert vp._english_neighbours("the", english) == set(), "self is not a neighbour"


# ---------------------------------------------------------------------------
# Degraded host
# ---------------------------------------------------------------------------


def test_no_typos_reported_without_a_real_dictionary(monkeypatch) -> None:
    """A 239-word list cannot tell a typo from vocabulary — report nothing.

    Emitting the operator's own vocabulary back to them as "your typos" is
    worse than an empty field.
    """
    monkeypatch.setattr(vp, "_ENGLISH_WORDS", frozenset({"the", "and", "for"}))
    corpus = ("waht taks thev rror " * 50) + ("node crashed running " * 50)
    assert vp._learn_typos(corpus) == []


def test_typos_found_when_a_dictionary_is_present() -> None:
    if not vp.english_wordlist_is_usable():
        import pytest

        pytest.skip("no system dictionary on this host")
    # _TYPO_MIN_FREQ requires a repeat, and the intended word must outnumber
    # the slip by _TYPO_DOMINANCE_RATIO — so 800 correct against 2 slips.
    corpus = ("the task had an error. " * 800) + ("taks rror " * 2)
    found = dict(vp._learn_typos(corpus))
    assert "taks" in found or "rror" in found, found
    for ordinary in ("task", "error", "the"):
        assert ordinary not in found
