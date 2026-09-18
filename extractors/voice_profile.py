"""Voice-profile extractor.

Measures the operator's *communication cadence* from their user turns so
future Claude sessions can match it instead of sounding like a stock LLM.

Companion to :mod:`extractors.operator_profile`: that one tells future
sessions WHO is asking, this one tells them HOW that person talks. Stored
in the ``voice_profile`` table (see :mod:`index.voice`) so the MCP layer
can serve it cheaply at session start.

The signal is computed on **natural** user turns only — short (``<400``
chars), plain-text ``content_kind="string"`` records, no XML-tag system
notifications. Pasted specs, code blocks and tool results are filtered
out before measurement: they drown the operator's actual voice in
copy-pasted formal prose.

Only standard-library code. Statistics are exact (sorted-index percentile
on the sampled list) — no numpy dependency.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "measure_voice",
    "measure_voice_incremental",
    "NATURAL_MAX_CHARS",
    "IMPERATIVE_FIRST_WORDS",
    "SIGNATURE_TYPO_CANDIDATES",
    "PROFANITY_WORDS",
    "_learn_typos",
]


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


# Anything longer than this is almost certainly a pasted spec / log / prompt
# template, not a natural turn. 400 chars matches the research filter that
# produced the cheat sheet in skills/speak-like-operator/SKILL.md.
NATURAL_MAX_CHARS = 400

# First-word imperatives — used to compute imperative_first_word_pct.
# Kept broad on purpose; "go", "try", "use" all qualify as command-style
# openers even though they're polysemous in English.
IMPERATIVE_FIRST_WORDS: frozenset[str] = frozenset(
    {
        "do",
        "check",
        "fix",
        "run",
        "use",
        "install",
        "deploy",
        "make",
        "add",
        "remove",
        "delete",
        "update",
        "build",
        "create",
        "look",
        "read",
        "find",
        "grep",
        "show",
        "tell",
        "give",
        "put",
        "set",
        "start",
        "stop",
        "restart",
        "pull",
        "push",
        "test",
        "verify",
        "try",
        "open",
        "close",
        "clean",
        "kill",
        "rerun",
        "redo",
        "continue",
        "keep",
        "go",
        "rebuild",
        "redeploy",
        "revert",
        "undo",
        "revisit",
        "investigate",
        "figure",
        "dig",
        "search",
        "write",
        "commit",
        "merge",
    }
)


# Signature typos are now LEARNED per-operator from the corpus; this
# constant is kept only as a small universal baseline of well-known
# common English misspellings used as an initial seed filter.
# Author-specific typos have been removed — they would never fire for
# any other operator and were a privacy tell in the published code.
SIGNATURE_TYPO_CANDIDATES: tuple[str, ...] = (
    "teh",
    "seperate",
    "recieve",
    "definately",
    "occured",
    "untill",
)


PROFANITY_WORDS: tuple[str, ...] = ("fuck", "shit", "wtf", "ffs", "bullshit")


# Counted as part of the we_vs_i ratio (numerator = sum of "we","us","our"
# whole-word hits; denominator = count of " i " whole-word hits, with min
# clamp 1 to avoid divide-by-zero).
_WE_PRONOUNS = ("we", "us", "our")


# ---------------------------------------------------------------------------
# Percentile helper (stdlib-only)
# ---------------------------------------------------------------------------


def _percentile(values: list[int] | list[float] | list[int | float], p: float) -> float:
    """Return the ``p``-th percentile of ``values`` (0 <= p <= 100).

    Nearest-rank method: index = floor(len*p/100), clamped to [0, len-1].
    Matches what the research script used; numpy not required.
    """
    if not values:
        return 0.0
    a = sorted(values)
    idx = int(len(a) * p / 100.0)
    if idx >= len(a):
        idx = len(a) - 1
    if idx < 0:
        idx = 0
    return float(a[idx])


def _mean(values: list[int] | list[float] | list[int | float]) -> float:
    if not values:
        return 0.0
    return float(sum(values)) / len(values)


# ---------------------------------------------------------------------------
# Record adapter — accept Record-like objects OR raw dicts
# ---------------------------------------------------------------------------


def _user_string_text(rec: Any) -> str | None:
    """Return the user-string payload if ``rec`` qualifies, else ``None``.

    Accepts three shapes:

    1. RecordLike *objects* exposing ``.type`` / ``.content_kind`` /
       ``.text`` directly (the standard :class:`lib.schema.Record`
       shape used by the pipeline).
    2. Pre-normalised *dicts* with the same top-level keys
       (``{"type": "user", "content_kind": "string", "text": "..."}``) —
       what tests use because it's cheaper than building a Record.
    3. Raw JSONL dicts as written by Claude Code, where the text lives at
       ``message.content`` (str) and there's no explicit ``content_kind``.

    The voice extractor wants natural turns only — anything starting
    with ``<`` (system notification, tool XML tag) or longer than
    :data:`NATURAL_MAX_CHARS` is rejected here.
    """
    if isinstance(rec, dict):
        rec_type = rec.get("type")
        content_kind = rec.get("content_kind")
        text = rec.get("text")
        if content_kind is None and text is None:
            # Shape 3: raw JSONL — pull from message.content.
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                content_kind = "string"
                text = content
            elif isinstance(content, list):
                content_kind = "list"
                text = None
    else:
        rec_type = getattr(rec, "type", None)
        content_kind = getattr(rec, "content_kind", None)
        text = getattr(rec, "text", None)

    if rec_type != "user":
        return None
    if content_kind != "string":
        return None
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("<"):
        # System notification, tool-result XML, command-name etc.
        return None
    if len(stripped) >= NATURAL_MAX_CHARS:
        return None
    return stripped


_WHOLE_WORD_CACHE: dict[str, re.Pattern[str]] = {}


def _whole_word_count(corpus: str, word: str) -> int:
    """Lowercased whole-word count of ``word`` in ``corpus``.

    Uses ``\\b`` boundaries so ``i`` doesn't match every word containing
    the letter i. Patterns are cached because the corpus iterates dozens
    of these at the bottom of the function.
    """
    pat = _WHOLE_WORD_CACHE.get(word)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(word)}\b")
        _WHOLE_WORD_CACHE[word] = pat
    return len(pat.findall(corpus))


def _first_word(text: str) -> str:
    """Return the lowercased, punctuation-stripped first word."""
    parts = text.split()
    if not parts:
        return ""
    return re.sub(r"[^a-z]", "", parts[0].lower())


# ---------------------------------------------------------------------------
# Data-driven typo discovery
# ---------------------------------------------------------------------------

# Minimum number of occurrences for a token to be considered a signature
# typo; keeps noise out when the corpus is small.
_TYPO_MIN_FREQ = 2

# Maximum number of learned typos to surface.
_TYPO_TOP_N = 30

# Patterns that disqualify a token from being a typo candidate:
# - looks like code / an identifier (contains digits or underscores)
# - looks like a URL or path fragment
# - too short (1-2 chars) or very long (>20 chars — probably a slug)
_SKIP_TOKEN = re.compile(r"[0-9_/\\@#$%^*+=<>|]|https?:|\.{2,}")

# Minimum token length (inclusive) to consider.
_TYPO_MIN_LEN = 3


# Probed in order; the first list with enough entries wins.
#   words / american-english / british-english  Debian, Fedora, macOS
#   cracklib-small                              Arch, Manjaro
#   web2                                        macOS / BSD
#   *.dic                                       hunspell / myspell
_SYSTEM_WORDLIST_PATHS: tuple[str, ...] = (
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/share/dict/british-english",
    "/usr/share/dict/web2",
    "/usr/share/dict/cracklib-small",
    "/usr/share/cracklib/cracklib-small",
    "/usr/share/hunspell/en_US.dic",
    "/usr/share/myspell/en_US.dic",
    "/usr/share/myspell/dicts/en_US.dic",
)

# A list smaller than this cannot tell a typo from ordinary vocabulary, so
# the typo feature reports nothing rather than guessing (see _learn_typos).
_MIN_USABLE_WORDLIST = 5_000


def _normalize_wordlist_entry(line: str) -> str:
    """Lowercase one wordlist line, dropping hunspell affix flags.

    hunspell/myspell ``.dic`` entries look like ``running/AG`` and the first
    line is an entry count, so the affix suffix has to come off or the word
    never matches.
    """
    word = line.strip()
    if not word:
        return ""
    word = word.split("/", 1)[0].split("\t", 1)[0]
    return word.lower() if word.isalpha() else ""


def english_wordlist_is_usable() -> bool:
    """True when a real system dictionary backs the typo filter.

    False means only the embedded fallback was found, which is too small to
    separate typos from vocabulary.
    """
    return len(_get_english_words()) >= _MIN_USABLE_WORDLIST


def _load_english_wordlist() -> frozenset[str]:
    """Return a frozenset of lowercase English words for the typo filter.

    Offline and dependency-free: probe the well-known system wordlists in
    turn, then fall back to the embedded set.

    Only ``/usr/share/dict/words`` used to be tried. That path does not exist
    on Arch/Manjaro (which ship ``dict/cracklib-small``) or on a Debian box
    without ``wordlist`` installed, so those hosts silently dropped to the
    embedded list — 239 words, not the ~600 the docstring claimed — and every
    ordinary word the operator typed came back as one of their "signature
    typos": ``node``, ``crashed``, ``running``, ``fleet``.
    """
    for path in _SYSTEM_WORDLIST_PATHS:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                words = frozenset(_normalize_wordlist_entry(line) for line in fh)
        except OSError:
            continue
        words = frozenset(w for w in words if w)
        if len(words) >= _MIN_USABLE_WORDLIST:
            return words

    # Embedded fallback: common English words + contractions + tech terms.
    # Not exhaustive — just broad enough to suppress normal vocabulary.
    _COMMON = (
        "a",
        "about",
        "above",
        "across",
        "add",
        "after",
        "again",
        "against",
        "ago",
        "all",
        "also",
        "although",
        "always",
        "and",
        "any",
        "are",
        "around",
        "as",
        "at",
        "back",
        "bad",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "build",
        "but",
        "by",
        "call",
        "can",
        "change",
        "check",
        "clean",
        "close",
        "code",
        "come",
        "config",
        "copy",
        "could",
        "create",
        "current",
        "data",
        "day",
        "dead",
        "debug",
        "delete",
        "deploy",
        "did",
        "do",
        "does",
        "done",
        "down",
        "each",
        "easy",
        "end",
        "error",
        "every",
        "fail",
        "false",
        "far",
        "file",
        "find",
        "first",
        "fix",
        "for",
        "from",
        "full",
        "get",
        "go",
        "going",
        "good",
        "got",
        "great",
        "had",
        "has",
        "have",
        "he",
        "help",
        "her",
        "here",
        "him",
        "his",
        "how",
        "if",
        "in",
        "install",
        "into",
        "is",
        "it",
        "its",
        "just",
        "keep",
        "key",
        "know",
        "last",
        "let",
        "like",
        "list",
        "local",
        "log",
        "long",
        "look",
        "make",
        "may",
        "me",
        "merge",
        "more",
        "most",
        "move",
        "much",
        "must",
        "my",
        "name",
        "new",
        "next",
        "no",
        "not",
        "now",
        "null",
        "of",
        "off",
        "ok",
        "on",
        "one",
        "only",
        "open",
        "or",
        "other",
        "our",
        "out",
        "over",
        "path",
        "pick",
        "port",
        "pull",
        "push",
        "put",
        "re",
        "read",
        "real",
        "remove",
        "rerun",
        "restart",
        "right",
        "run",
        "same",
        "see",
        "server",
        "set",
        "should",
        "show",
        "since",
        "so",
        "some",
        "start",
        "still",
        "stop",
        "sure",
        "take",
        "test",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "through",
        "time",
        "to",
        "todo",
        "too",
        "true",
        "try",
        "two",
        "under",
        "up",
        "update",
        "use",
        "used",
        "using",
        "very",
        "via",
        "want",
        "was",
        "way",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "work",
        "write",
        "yes",
        "yet",
        "you",
        "your",
        # Contractions (without apostrophe, as text is lowercased).
        "dont",
        "doesnt",
        "didnt",
        "cant",
        "wont",
        "isnt",
        "wasnt",
        "wouldnt",
        "couldnt",
        "shouldnt",
        "havent",
        "hasnt",
        "hadnt",
        "im",
        "youre",
        "theyre",
        "weve",
        "its",
        # Common informal forms that would otherwise look like typos.
        "tho",
        "cos",
        "cus",
        "ngl",
        "tbh",
        "ok",
        "okay",
        "yeah",
        "yep",
        "nope",
        "rly",
        "btw",
        "fyi",
    )
    return frozenset(_COMMON)


# Cache the wordlist so we only load it once per process lifetime.
_ENGLISH_WORDS: frozenset[str] | None = None


def _get_english_words() -> frozenset[str]:
    global _ENGLISH_WORDS
    if _ENGLISH_WORDS is None:
        _ENGLISH_WORDS = _load_english_wordlist()
    return _ENGLISH_WORDS


_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Shorter than this and a token is an acronym, not a slip.
_TYPO_MIN_NEAR_LEN = 4

# How much commoner the intended word must be. Calibrated on a 3.3M-char
# operator corpus: below ~200x the list fills with technical abbreviations
# (perf/repo/auth/toml), at 200x it is dominated by real slips.
_TYPO_DOMINANCE_RATIO = 200

# Cap when the intended word is absent from the corpus and cannot vouch for
# the candidate. Deliberately tight: vocabulary recurs, a slip does not.
_TYPO_MAX_UNCORROBORATED = 5


def _english_neighbours(token: str, english: frozenset[str]) -> set[str]:
    """Real words one edit from ``token`` — deletion, swap, substitution, insertion."""
    out: set[str] = set()
    for i in range(len(token)):
        out.add(token[:i] + token[i + 1 :])
    for i in range(len(token) - 1):
        out.add(token[:i] + token[i + 1] + token[i] + token[i + 2 :])
    for i in range(len(token)):
        for ch in _ALPHABET:
            out.add(token[:i] + ch + token[i + 1 :])
    for i in range(len(token) + 1):
        for ch in _ALPHABET:
            out.add(token[:i] + ch + token[i:])
    out.discard(token)
    return {w for w in out if w in english}


def _looks_like_typo(
    token: str, count: int, english: frozenset[str], corpus_counts: Counter[str]
) -> bool:
    """True when ``token`` reads as a slip rather than vocabulary.

    Two conditions, both needed:

    * one edit from a real word — that is what a typo *is*; and
    * the word it misses is *far* commoner in the same corpus.

    The second is what separates a slip from jargon, and neither test alone
    does. Absence from the dictionary catches nothing on its own (``gaudi``,
    ``nixl``, ``vllm`` are vocabulary, not misspellings), and nearness is
    almost free for short tokens — ``gaudi`` is one substitution from
    ``gaudy``, ``hpu`` one from ``cpu``. But nobody mistypes the same word
    ten thousand times: measured here ``gaudi`` outnumbers ``gaudy`` and
    ``harvestd`` runs at a third of ``harvest``, while a true slip like
    ``waht`` or ``taks`` is hundreds of times rarer than the word meant.
    """
    if len(token) < _TYPO_MIN_NEAR_LEN:
        # Short tokens are acronyms far more often than slips, and they sit
        # one edit from half the dictionary.
        return False
    neighbours = _english_neighbours(token, english)
    if not neighbours:
        return False
    # Either signal is enough on its own.
    #
    # Dominance handles a token used often enough to look like vocabulary:
    # it is still a slip if the word it misses is hundreds of times commoner.
    # Rarity handles the rest, including the case where the intended word is
    # absent or barely present, so dominance has nothing to weigh — a writer
    # who only ever gets a word wrong still made a typo. Vocabulary recurs
    # and so fails both: `gaudi` ran to ten thousand uses here, `harvestd`
    # to hundreds, while a slip stays in single figures.
    best = max(corpus_counts.get(w, 0) for w in neighbours)
    if count <= _TYPO_MAX_UNCORROBORATED:
        return True
    return best >= _TYPO_DOMINANCE_RATIO * count


def _learn_typos(
    corpus_lc: str,
    top_n: int = _TYPO_TOP_N,
    min_freq: int = _TYPO_MIN_FREQ,
) -> list[tuple[str, int]]:
    """Discover the operator's personal typos from their lowercased corpus.

    Algorithm:
    1. Tokenise the corpus into alphabetic-only tokens (length >= 3).
    2. Discard any token that is a known English word (system wordlist or
       embedded fallback).
    3. Discard tokens that look like code, paths, or identifiers via
       _SKIP_TOKEN heuristic.
    4. Count survivors; keep those appearing >= min_freq times.
    5. Return the top_n most frequent as [(typo, count)] sorted desc by count.

    The universal SIGNATURE_TYPO_CANDIDATES seed is also always scanned
    (even if they fall below min_freq) so baseline well-known misspellings
    are never silently dropped.
    """
    english = _get_english_words()

    # No real dictionary on this host: the embedded fallback cannot tell a
    # typo from ordinary vocabulary, so every word the operator wrote would
    # be reported as their typo. Report nothing instead of nonsense.
    if len(english) < _MIN_USABLE_WORDLIST:
        return []

    # Tokenise: split on anything that's not alpha, keep length >= min_len.
    raw_tokens = re.findall(rf"[a-z]{{{_TYPO_MIN_LEN},}}", corpus_lc)

    # First pass: count candidate tokens.
    token_counts: Counter[str] = Counter()
    for tok in raw_tokens:
        # Quick-reject: known word or too long (likely a slug).
        if tok in english or len(tok) > 20:
            continue
        token_counts[tok] += 1

    # Build learned list: must recur >= min_freq *and* be one edit from a
    # real word. Frequency alone promotes domain jargon (gaudi, vllm, nixl)
    # that is simply absent from the dictionary, not misspelled.
    # Every alphabetic token, including dictionary words — needed as the
    # denominator when judging whether a candidate is a slip off one of them.
    corpus_counts: Counter[str] = Counter(raw_tokens)
    learned: Counter[str] = Counter(
        {
            tok: cnt
            for tok, cnt in token_counts.items()
            if cnt >= min_freq and _looks_like_typo(tok, cnt, english, corpus_counts)
        }
    )

    # Always fold in the universal seed (SIGNATURE_TYPO_CANDIDATES) at
    # whatever count they have, even if below threshold — they are canonical
    # common misspellings and we want them if present at all.
    for seed_typo in SIGNATURE_TYPO_CANDIDATES:
        c = corpus_lc.count(seed_typo)
        if c > 0:
            learned[seed_typo] = max(learned.get(seed_typo, 0), c)

    return learned.most_common(top_n)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def measure_voice(records: Iterable[Any]) -> dict[str, Any]:
    """Scan ``records`` and return a ``voice_profile`` field dict.

    Only user-string records (``content_kind == "string"``) shorter than
    :data:`NATURAL_MAX_CHARS` and not starting with ``<`` are measured.
    The return shape matches the schema documented in :mod:`index.voice`,
    plus a ``sample_size`` key the caller passes through to
    :func:`index.voice.persist_voice_profile`.

    All-zero / empty corpus → every numeric stat is ``0.0`` or ``0``,
    typos / first-words lists are empty. ``sample_size`` is ``0``. This
    keeps the table writable on an empty index (so the MCP tool returns
    a stable empty shape instead of "voice not yet mined").
    """
    turns: list[str] = []
    for rec in records:
        t = _user_string_text(rec)
        if t is not None:
            turns.append(t)

    n = len(turns)
    if n == 0:
        return {
            "lowercase_start_pct": 0.0,
            "mean_chars": 0,
            "chars_p10": 0,
            "chars_p50": 0,
            "chars_p90": 0,
            "mean_tokens": 0.0,
            "tokens_p10": 0,
            "tokens_p50": 0,
            "tokens_p90": 0,
            "ends_period_pct": 0.0,
            "ends_question_pct": 0.0,
            "ends_no_punct_pct": 0.0,
            "imperative_first_word_pct": 0.0,
            "signature_typos": [],
            "top_first_words": [],
            "we_vs_i_ratio": 0.0,
            "profanity_per_1k_turns": 0.0,
            "one_word_turn_pct": 0.0,
            "sample_size": 0,
        }

    # --- Casing ----------------------------------------------------------
    lc_count = sum(1 for t in turns if t and t[0].islower())
    lowercase_start_pct = lc_count / n

    # --- Length distribution --------------------------------------------
    char_lens = [len(t) for t in turns]
    token_lens = [len(t.split()) for t in turns]

    # --- Endings ---------------------------------------------------------
    ends_period = sum(1 for t in turns if t.endswith("."))
    ends_question = sum(1 for t in turns if t.endswith("?"))
    ends_no_punct = sum(1 for t in turns if t and t[-1] not in ".?!,:;")

    # --- Imperative density ---------------------------------------------
    first_words = [_first_word(t) for t in turns]
    imperative_n = sum(1 for fw in first_words if fw in IMPERATIVE_FIRST_WORDS)
    imperative_pct = imperative_n / n

    # Top first words: drop the empty-string bucket (lines that lost their
    # opener after the punctuation strip). 15 is the same cap as the
    # research script.
    fw_counter = Counter(fw for fw in first_words if fw)
    top_first_words = fw_counter.most_common(15)

    # --- One-word turns -------------------------------------------------
    one_word_n = sum(1 for tl in token_lens if tl == 1)
    one_word_pct = one_word_n / n

    # --- Pronoun ratio --------------------------------------------------
    corpus_lc = " ".join(t.lower() for t in turns)
    we_total = sum(_whole_word_count(corpus_lc, w) for w in _WE_PRONOUNS)
    i_total = _whole_word_count(corpus_lc, "i")
    # Avoid div-by-zero; ratio of 0 means "no 'I' usage observed" so
    # we_vs_i_ratio = we_total when i_total is 0, capped at the raw count.
    we_vs_i_ratio = float(we_total) / float(i_total) if i_total > 0 else float(we_total)

    # --- Profanity rate (per 1000 turns) -------------------------------
    profanity_hits = 0
    for w in PROFANITY_WORDS:
        profanity_hits += _whole_word_count(corpus_lc, w)
    profanity_per_1k_turns = (profanity_hits * 1000.0) / n

    # --- Signature typos (data-driven, per-operator) --------------------
    # Typos are LEARNED from the operator's own corpus rather than
    # hardcoded — so the signal is personalised and works for any operator.
    # _learn_typos filters non-English tokens that recur above a threshold;
    # the universal SIGNATURE_TYPO_CANDIDATES seed is always included if
    # present. Author-specific typos have been removed.
    typo_counts: list[tuple[str, int]] = _learn_typos(corpus_lc)

    return {
        "lowercase_start_pct": round(lowercase_start_pct, 4),
        "mean_chars": int(round(_mean(char_lens))),
        "chars_p10": int(_percentile(char_lens, 10)),
        "chars_p50": int(_percentile(char_lens, 50)),
        "chars_p90": int(_percentile(char_lens, 90)),
        "mean_tokens": round(_mean(token_lens), 2),
        "tokens_p10": int(_percentile(token_lens, 10)),
        "tokens_p50": int(_percentile(token_lens, 50)),
        "tokens_p90": int(_percentile(token_lens, 90)),
        "ends_period_pct": round(ends_period / n, 4),
        "ends_question_pct": round(ends_question / n, 4),
        "ends_no_punct_pct": round(ends_no_punct / n, 4),
        "imperative_first_word_pct": round(imperative_pct, 4),
        "signature_typos": typo_counts,
        "top_first_words": top_first_words,
        "we_vs_i_ratio": round(we_vs_i_ratio, 3),
        "profanity_per_1k_turns": round(profanity_per_1k_turns, 3),
        "one_word_turn_pct": round(one_word_pct, 4),
        "sample_size": n,
    }


# ---------------------------------------------------------------------------
# Incremental EMA-blended update (Stop-hook hot path)
# ---------------------------------------------------------------------------


# Numeric fields that participate in the EMA blend. Ints are blended as
# floats and rounded back. Distributional stats (chars_p10/50/90,
# tokens_p10/50/90) are included here too — without persisted raw samples
# we approximate "rolling reservoir" via EMA, which converges to the same
# steady state in expectation. The weekly cron (CW8) can recompute exact
# values from the full corpus if drift becomes an issue.
_EMA_FLOAT_FIELDS: tuple[str, ...] = (
    "lowercase_start_pct",
    "ends_period_pct",
    "ends_question_pct",
    "ends_no_punct_pct",
    "imperative_first_word_pct",
    "we_vs_i_ratio",
    "profanity_per_1k_turns",
    "one_word_turn_pct",
    "mean_tokens",
)
_EMA_INT_FIELDS: tuple[str, ...] = (
    "mean_chars",
    "chars_p10",
    "chars_p50",
    "chars_p90",
    "tokens_p10",
    "tokens_p50",
    "tokens_p90",
)

# Count-style fields that accumulate via Counter merge instead of EMA.
_COUNTER_FIELDS: tuple[str, ...] = ("signature_typos", "top_first_words")


def _merge_counter_pairs(
    existing: list[Any] | None,
    incoming: list[Any] | None,
    cap: int,
) -> list[tuple[str, int]]:
    """Merge two ``[(item, count), ...]`` lists into a frequency-sorted result."""
    c: Counter[str] = Counter()
    for src in (existing or [], incoming or []):
        for pair in src:
            try:
                term, hits = pair[0], int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if term:
                c[term] += hits
    return c.most_common(cap)


def measure_voice_incremental(
    new_records: Iterable[Any],
    existing: dict[str, Any] | None = None,
    window_size: int = 200,
) -> dict[str, Any]:
    """Update voice profile via EMA over a rolling window.

    The EMA weight for the batch is ``alpha = min(1.0, batch_size /
    window_size)``: an empty existing profile (or a batch larger than the
    window) snaps to the batch values; tiny batches drift the existing
    values gently. Counter-style fields (``signature_typos``,
    ``top_first_words``) are accumulated as union-counts so rare typos
    aren't washed out by the EMA.

    ``existing`` is the dict returned by a previous
    :func:`measure_voice` / :func:`measure_voice_incremental` call (or
    by :func:`index.voice.get_voice`). Reserved keys starting with ``_``
    are ignored on input.
    """
    batch = measure_voice(new_records)
    batch_n = int(batch.get("sample_size", 0) or 0)

    if not existing or not existing.get("sample_size"):
        # Cold-start: the batch IS the profile.
        return batch
    if batch_n == 0:
        # No new natural turns this round — nothing to merge.
        return dict(existing)

    alpha = min(1.0, batch_n / float(window_size))

    out: dict[str, Any] = dict(existing)
    # Strip sidecar keys so we don't emit them back out.
    for k in ("_measured_at", "_sample_size"):
        out.pop(k, None)

    for key in _EMA_FLOAT_FIELDS:
        old = float(existing.get(key, 0.0) or 0.0)
        new = float(batch.get(key, 0.0) or 0.0)
        blended = alpha * new + (1.0 - alpha) * old
        out[key] = round(blended, 4)

    for key in _EMA_INT_FIELDS:
        old = float(existing.get(key, 0) or 0)
        new = float(batch.get(key, 0) or 0)
        blended = alpha * new + (1.0 - alpha) * old
        out[key] = int(round(blended))

    for key in _COUNTER_FIELDS:
        # Different cap for first-words vs typos (matches measure_voice).
        cap = 15 if key == "top_first_words" else 50
        out[key] = _merge_counter_pairs(existing.get(key), batch.get(key), cap)

    # Rolling sample_size — clamp at window_size so it doesn't grow forever.
    prior_n = int(existing.get("sample_size", 0) or 0)
    out["sample_size"] = min(window_size, prior_n + batch_n)

    return out
