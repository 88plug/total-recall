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
from typing import Any, Iterable

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
# produced the cheat sheet in skills/speak-like-andrew/SKILL.md.
NATURAL_MAX_CHARS = 400

# First-word imperatives — used to compute imperative_first_word_pct.
# Kept broad on purpose; "go", "try", "use" all qualify as command-style
# openers even though they're polysemous in English.
IMPERATIVE_FIRST_WORDS: frozenset[str] = frozenset(
    {
        "do", "check", "fix", "run", "use", "install", "deploy",
        "make", "add", "remove", "delete", "update", "build", "create",
        "look", "read", "find", "grep", "show", "tell", "give", "put",
        "set", "start", "stop", "restart", "pull", "push", "test",
        "verify", "try", "open", "close", "clean", "kill", "rerun",
        "redo", "continue", "keep", "go", "rebuild", "redeploy", "revert",
        "undo", "revisit", "investigate", "figure", "dig", "search",
        "write", "commit", "merge",
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


def _percentile(values: list[int | float], p: float) -> float:
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


def _mean(values: list[int | float]) -> float:
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


def _load_english_wordlist() -> frozenset[str]:
    """Return a frozenset of lowercase English words for the typo filter.

    Strategy (offline, dependency-free, KISS):
    1. Try the system wordlist at /usr/share/dict/words — present on most
       Linux/macOS installs. Contains ~100k+ entries and handles obscure
       technical vocabulary well.
    2. Fall back to an embedded set of ~600 high-frequency English words
       that covers the common vocabulary of engineering chat (verbs, nouns,
       pronouns, conjunctions, prepositions, common adj/adv).  The embedded
       set is intentionally large enough that normal words almost never pass
       through as "typos" even without the system list.
    """
    system_path = "/usr/share/dict/words"
    try:
        with open(system_path, encoding="utf-8", errors="ignore") as fh:
            words = frozenset(w.strip().lower() for w in fh if w.strip())
        if len(words) > 1000:  # sanity check — file must be non-trivial
            return words
    except OSError:
        pass

    # Embedded fallback: common English words + contractions + tech terms.
    # Not exhaustive — just broad enough to suppress normal vocabulary.
    _COMMON = (
        "a", "about", "above", "across", "add", "after", "again", "against",
        "ago", "all", "also", "although", "always", "and", "any", "are",
        "around", "as", "at", "back", "bad", "be", "because", "been",
        "before", "being", "below", "between", "both", "build", "but", "by",
        "call", "can", "change", "check", "clean", "close", "code", "come",
        "config", "copy", "could", "create", "current", "data", "day", "dead",
        "debug", "delete", "deploy", "did", "do", "does", "done", "down",
        "each", "easy", "end", "error", "every", "fail", "false", "far",
        "file", "find", "first", "fix", "for", "from", "full", "get", "go",
        "going", "good", "got", "great", "had", "has", "have", "he", "help",
        "her", "here", "him", "his", "how", "if", "in", "install", "into",
        "is", "it", "its", "just", "keep", "key", "know", "last", "let",
        "like", "list", "local", "log", "long", "look", "make", "may", "me",
        "merge", "more", "most", "move", "much", "must", "my", "name", "new",
        "next", "no", "not", "now", "null", "of", "off", "ok", "on", "one",
        "only", "open", "or", "other", "our", "out", "over", "path", "pick",
        "port", "pull", "push", "put", "re", "read", "real", "remove", "rerun",
        "restart", "right", "run", "same", "see", "server", "set", "should",
        "show", "since", "so", "some", "start", "still", "stop", "sure",
        "take", "test", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "through", "time", "to", "todo",
        "too", "true", "try", "two", "under", "up", "update", "use", "used",
        "using", "very", "via", "want", "was", "way", "we", "were", "what",
        "when", "where", "which", "while", "who", "why", "will", "with",
        "work", "write", "yes", "yet", "you", "your",
        # Contractions (without apostrophe, as text is lowercased).
        "dont", "doesnt", "didnt", "cant", "wont", "isnt", "wasnt",
        "wouldnt", "couldnt", "shouldnt", "havent", "hasnt", "hadnt",
        "im", "youre", "theyre", "weve", "its",
        # Common informal forms that would otherwise look like typos.
        "tho", "cos", "cus", "ngl", "tbh", "ok", "okay", "yeah", "yep",
        "nope", "rly", "btw", "fyi",
    )
    return frozenset(_COMMON)


# Cache the wordlist so we only load it once per process lifetime.
_ENGLISH_WORDS: frozenset[str] | None = None


def _get_english_words() -> frozenset[str]:
    global _ENGLISH_WORDS
    if _ENGLISH_WORDS is None:
        _ENGLISH_WORDS = _load_english_wordlist()
    return _ENGLISH_WORDS


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

    # Tokenise: split on anything that's not alpha, keep length >= min_len.
    raw_tokens = re.findall(r"[a-z]{%d,}" % _TYPO_MIN_LEN, corpus_lc)

    # First pass: count candidate tokens.
    token_counts: Counter[str] = Counter()
    for tok in raw_tokens:
        # Quick-reject: known word or too long (likely a slug).
        if tok in english or len(tok) > 20:
            continue
        token_counts[tok] += 1

    # Build learned list: must recur >= min_freq.
    learned: Counter[str] = Counter(
        {tok: cnt for tok, cnt in token_counts.items() if cnt >= min_freq}
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
    ends_no_punct = sum(
        1 for t in turns if t and t[-1] not in ".?!,:;"
    )

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
    "chars_p10", "chars_p50", "chars_p90",
    "tokens_p10", "tokens_p50", "tokens_p90",
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
        out[key] = _merge_counter_pairs(
            existing.get(key), batch.get(key), cap
        )

    # Rolling sample_size — clamp at window_size so it doesn't grow forever.
    prior_n = int(existing.get("sample_size", 0) or 0)
    out["sample_size"] = min(window_size, prior_n + batch_n)

    return out
