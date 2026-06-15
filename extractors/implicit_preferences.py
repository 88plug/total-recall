"""Implicit-preference extractor.

Derives operator preferences from *observed behavior* rather than from
explicit statements. Each preference is a ``(category, value)`` pair with
a confidence score and cross-session evidence counts.

The detection logic is entirely generic — no operator-specific values are
hardcoded. The VALUES emitted are derived from the operator's actual corpus:

* ``edit_strategy`` — ratio of Edit-tool calls to Write-tool calls across
  assistant turns. When Edit dominates by ≥ 2×, emit
  ``edit_strategy=prefer_edit``.
* ``shell_command`` — for every tool name in bash calls, count occurrences
  and check for dominant vs absent alternatives within the same functional
  group (package managers, container runtimes, SSH, etc.). Emits
  ``shell_command=prefer_<winner>`` when winner dominates.
* ``format`` — emoji presence in user turns (Unicode emoji ranges).
  If the operator almost never uses emoji across many turns → emit
  ``format=no_emojis_in_chat``.
* ``vocabulary`` — recurring N-gram phrases in user turns that cross a
  frequency threshold; emitted as ``category=vocabulary, value=<phrase>``.

A behavior is promoted to an inferred preference when:

* ``evidence_sessions ≥ 5``
* ``evidence_projects ≥ 3``
* ``contradiction_count == 0  OR
  evidence_sessions / (evidence_sessions + contradiction_count) ≥ 0.80``
* stability ≥ 7 days (first-seen to last-seen as unix timestamps)

Only stdlib is used. No LLM. No operator-specific literals.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "ImplicitPreference",
    "ImplicitPreferenceProfile",
    "extract_implicit_preferences",
    "extract_implicit_preferences_incremental",
    "PROMOTION_MIN_SESSIONS",
    "PROMOTION_MIN_PROJECTS",
    "PROMOTION_MIN_CONFIDENCE_RATIO",
    "PROMOTION_MIN_STABILITY_DAYS",
]


# ---------------------------------------------------------------------------
# Promotion thresholds (tunables)
# ---------------------------------------------------------------------------

PROMOTION_MIN_SESSIONS: int = 5
PROMOTION_MIN_PROJECTS: int = 3
PROMOTION_MIN_CONFIDENCE_RATIO: float = 0.80  # evidence / (evidence + contradictions)
PROMOTION_MIN_STABILITY_DAYS: int = 7

# Edit vs Write: if Edit/Write ratio exceeds this, emit preference.
_EDIT_WRITE_RATIO_THRESHOLD: float = 2.0

# Bash command dominance: a command must cover this fraction of all calls in
# its functional group (and the group must have been seen enough times).
_CMD_GROUP_MIN_CALLS: int = 5
_CMD_GROUP_DOMINANCE_THRESHOLD: float = 0.85

# Emoji: if emoji-containing turns are < this fraction of all natural turns → no_emojis.
_EMOJI_ABSENT_THRESHOLD: float = 0.05

# Vocabulary: N-gram parameters.
_NGRAM_N: int = 2  # bigrams by default; also scan trigrams
_VOCAB_MIN_SESSIONS: int = 3  # minimum sessions for a phrase to be considered at all
_VOCAB_TOP_K: int = 20  # keep only top-K by session count for efficiency

# Natural user turn filter (mirrors voice_profile).
_NATURAL_MAX_CHARS: int = 400

# ---------------------------------------------------------------------------
# Functional command groups (generic — any command that's in none of these
# groups is not grouped and won't be emitted as a preference).
# ---------------------------------------------------------------------------

# Each group: one list of mutually-exclusive alternatives.
_COMMAND_GROUPS: list[list[str]] = [
    # Python package managers
    ["uv", "pip", "poetry", "pipenv", "conda", "mamba", "pdm"],
    # JS package managers
    ["pnpm", "yarn", "npm", "bun"],
    # Container runtimes
    ["docker", "podman", "nerdctl"],
    # Kubernetes
    ["kubectl", "k3s", "k9s", "helm"],
    # SSH / remote shells
    ["ssh", "mosh"],
    # Process managers
    ["systemctl", "supervisorctl", "pm2", "s6-svc"],
    # Database CLIs
    ["psql", "mysql", "sqlite3", "mongosh"],
    # VCS
    ["git", "hg", "svn"],
    # Config management
    ["ansible", "puppet", "chef", "salt"],
    # Terraform ecosystem
    ["terraform", "tofu", "pulumi"],
]

# Build a fast lookup: command → group index.
_CMD_TO_GROUP: dict[str, int] = {}
for _gi, _grp in enumerate(_COMMAND_GROUPS):
    for _cmd in _grp:
        _CMD_TO_GROUP[_cmd] = _gi


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ImplicitPreference:
    category: str           # e.g. "edit_strategy", "tool_choice", "shell_command", "vocabulary"
    value: str              # e.g. "prefer_edit", "prefer_uv"
    confidence: float       # 0.0..1.0
    evidence_sessions: int  # number of distinct sessions exhibiting pattern
    evidence_projects: int  # number of distinct cwds
    contradiction_count: int  # times operator did the opposite
    sample_phrases: list[str] = field(default_factory=list)  # ≤3 raw short quotes

    def is_promoted(
        self,
        first_seen_ts: float | None = None,
        last_seen_ts: float | None = None,
    ) -> bool:
        """Return True when all promotion criteria are met."""
        if self.evidence_sessions < PROMOTION_MIN_SESSIONS:
            return False
        if self.evidence_projects < PROMOTION_MIN_PROJECTS:
            return False
        total = self.evidence_sessions + self.contradiction_count
        ratio = self.evidence_sessions / total if total > 0 else 0.0
        if self.contradiction_count > 0 and ratio < PROMOTION_MIN_CONFIDENCE_RATIO:
            return False
        # Stability check: need ≥ 7 days between first and last observation.
        if first_seen_ts is not None and last_seen_ts is not None:
            span_days = (last_seen_ts - first_seen_ts) / 86400.0
            if span_days < PROMOTION_MIN_STABILITY_DAYS:
                return False
        return True


@dataclass
class ImplicitPreferenceProfile:
    preferences: list[ImplicitPreference]
    sample_size: int  # sessions analyzed


# ---------------------------------------------------------------------------
# Helpers — record access (accepts raw JSONL dicts or attribute objects)
# ---------------------------------------------------------------------------


def _get(rec: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict/object by keys; return default on any miss."""
    cur: Any = rec
    for k in keys:
        cur = cur.get(k, default) if isinstance(cur, dict) else getattr(cur, k, default)
        if cur is None:
            return default
    return cur


def _is_assistant(rec: Any) -> bool:
    t = _get(rec, "type")
    return t == "assistant"


def _is_user_string(rec: Any) -> str | None:
    """Return user natural-turn text or None."""
    t = _get(rec, "type")
    if t != "user":
        return None
    content_kind = _get(rec, "content_kind")
    text = _get(rec, "text")
    if content_kind is None and text is None:
        msg = _get(rec, "message") or {}
        content = _get(msg, "content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            text = content
        else:
            return None
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or stripped.startswith("<") or len(stripped) >= _NATURAL_MAX_CHARS:
        return None
    return stripped


def _tool_uses(rec: Any) -> list[dict]:
    """Return all tool_use blocks from an assistant record."""
    content = _get(rec, "message", "content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _session_cwd(rec: Any) -> tuple[str, str]:
    """Return (session_id, cwd) from a record."""
    sid = _get(rec, "sessionId") or _get(rec, "session_id") or ""
    cwd = _get(rec, "cwd") or ""
    return str(sid), str(cwd)


def _timestamp(rec: Any) -> float | None:
    ts = _get(rec, "timestamp")
    if ts is None:
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Emoji detection (stdlib unicodedata)
# ---------------------------------------------------------------------------

# Unicode blocks that are primarily emoji / pictographs.
_EMOJI_RANGES: list[tuple[int, int]] = [
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F300, 0x1F5FF),  # Misc symbols and pictographs
    (0x1F680, 0x1F6FF),  # Transport and map
    (0x1F700, 0x1F77F),  # Alchemical symbols
    (0x1F780, 0x1F7FF),  # Geometric shapes extended
    (0x1F800, 0x1F8FF),  # Supplemental arrows-C
    (0x1F900, 0x1F9FF),  # Supplemental symbols and pictographs
    (0x1FA00, 0x1FA6F),  # Chess symbols
    (0x1FA70, 0x1FAFF),  # Symbols and pictographs extended-A
    (0x2600, 0x26FF),    # Misc symbols
    (0x2700, 0x27BF),    # Dingbats
    (0xFE00, 0xFE0F),    # Variation selectors
    (0x1F1E0, 0x1F1FF),  # Flags
]


def _has_emoji(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        for lo, hi in _EMOJI_RANGES:
            if lo <= cp <= hi:
                return True
        # Also catch category So (other symbol) and Sm (math symbol) chars
        # that are commonly emoji-adjacent.
        cat = unicodedata.category(ch)
        if cat in ("So", "Sm") and cp > 0x2000:
            return True
    return False


# ---------------------------------------------------------------------------
# N-gram extractor
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z']+")


def _ngrams(text: str, n: int) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < n:
        return []
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


# ---------------------------------------------------------------------------
# Per-session accumulator (intermediate state before promotion)
# ---------------------------------------------------------------------------


class _SessionAccumulator:
    """Accumulates raw counts across records in a single session pass."""

    def __init__(self) -> None:
        # edit_strategy: per-session edit vs write counts
        self.edit_calls: int = 0
        self.write_calls: int = 0

        # shell_command: per-group, per-command counts within this session
        self.cmd_counts: Counter[str] = Counter()  # command name → count

        # format: emoji presence
        self.emoji_turns: int = 0
        self.natural_turns: int = 0

        # vocabulary: phrase → sessions seen (accumulated later)
        self.ngram_counts: Counter[str] = Counter()

        self.cwd: str = ""
        self.session_id: str = ""
        self.first_ts: float | None = None
        self.last_ts: float | None = None

    def feed(self, rec: Any) -> None:
        sid, cwd = _session_cwd(rec)
        if not self.session_id:
            self.session_id = sid
        if not self.cwd:
            self.cwd = cwd

        ts = _timestamp(rec)
        if ts is not None:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts

        if _is_assistant(rec):
            for block in _tool_uses(rec):
                name = (block.get("name") or "").strip().lower()
                if name == "edit":
                    self.edit_calls += 1
                elif name == "write":
                    self.write_calls += 1
                elif name in ("bash", "shell", "run_bash"):
                    # Extract the first word of the command as the command name.
                    inp = block.get("input") or {}
                    cmd_text = ""
                    if isinstance(inp, dict):
                        cmd_text = inp.get("command", "") or inp.get("cmd", "")
                    elif isinstance(inp, str):
                        cmd_text = inp
                    first_word = cmd_text.strip().split()[0].lower() if cmd_text.strip() else ""
                    if first_word:
                        self.cmd_counts[first_word] += 1

        text = _is_user_string(rec)
        if text is not None:
            self.natural_turns += 1
            if _has_emoji(text):
                self.emoji_turns += 1
            # N-grams (bigrams + trigrams)
            for n in (2, 3):
                for gram in _ngrams(text, n):
                    self.ngram_counts[gram] += 1


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


def extract_implicit_preferences(
    jsonl_paths: Iterable[Any],
) -> ImplicitPreferenceProfile:
    """Full-corpus extraction from a list of (session_id, cwd, records) tuples
    OR from a list of path-like objects (opened and iterated line by line).

    Accepts two input shapes:
    1. An iterable of ``(session_id, cwd, records_iterable)`` triples —
       used by tests and the ingest pipeline when records are already parsed.
    2. An iterable of path-like objects (str or Path) — each file is opened
       and each line is parsed as JSON (raw JSONL shape).

    Returns an :class:`ImplicitPreferenceProfile` where only promoted
    preferences are included (stability check uses first/last session timestamps
    across the corpus).
    """
    import pathlib

    sessions: list[_SessionAccumulator] = []

    for item in jsonl_paths:
        # Shape 1: (session_id, cwd, records) triple
        if isinstance(item, (list, tuple)) and len(item) == 3:
            sid, cwd, records = item
            acc = _SessionAccumulator()
            acc.session_id = str(sid)
            acc.cwd = str(cwd)
            for rec in records:
                acc.feed(rec)
            sessions.append(acc)
        # Shape 2: path-like
        elif isinstance(item, (str, pathlib.Path)):
            path = pathlib.Path(item)
            try:
                acc = _SessionAccumulator()
                with path.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        acc.feed(rec)
                sessions.append(acc)
            except OSError as e:
                log.warning("implicit_prefs: cannot open %s: %s", item, e)

    return _build_profile(sessions)


def extract_implicit_preferences_incremental(
    records: Iterable[Any],
    existing: ImplicitPreferenceProfile | None,
    *,
    session_id: str = "",
    cwd: str = "",
) -> ImplicitPreferenceProfile:
    """Accumulate evidence from a new batch of records.

    ``existing`` carries the prior profile; the new session's signals are
    merged with the existing preference evidence counts, then promotion is
    re-evaluated. This is the Stop-hook hot path.
    """
    acc = _SessionAccumulator()
    if session_id:
        acc.session_id = session_id
    if cwd:
        acc.cwd = cwd
    for rec in records:
        acc.feed(rec)

    # Build a synthetic "one new session" profile, then merge with existing.
    new_profile = _build_profile([acc])

    if existing is None or not existing.preferences:
        return new_profile

    # Merge new evidence into existing preferences (or add new ones).
    merged = _merge_profiles(existing, new_profile)
    return merged


def _merge_profiles(
    base: ImplicitPreferenceProfile,
    incoming: ImplicitPreferenceProfile,
) -> ImplicitPreferenceProfile:
    """Merge two profiles, summing evidence counts."""
    index: dict[tuple[str, str], ImplicitPreference] = {
        (p.category, p.value): p for p in base.preferences
    }
    for pref in incoming.preferences:
        key = (pref.category, pref.value)
        if key in index:
            existing = index[key]
            # Sum evidence, pick max confidence.
            merged_pref = ImplicitPreference(
                category=pref.category,
                value=pref.value,
                confidence=max(existing.confidence, pref.confidence),
                evidence_sessions=existing.evidence_sessions + pref.evidence_sessions,
                evidence_projects=max(existing.evidence_projects, pref.evidence_projects),
                contradiction_count=existing.contradiction_count + pref.contradiction_count,
                sample_phrases=list(
                    dict.fromkeys(existing.sample_phrases + pref.sample_phrases)
                )[:3],
            )
            index[key] = merged_pref
        else:
            index[key] = pref

    total_sessions = base.sample_size + incoming.sample_size
    return ImplicitPreferenceProfile(
        preferences=list(index.values()),
        sample_size=total_sessions,
    )


def _build_profile(sessions: list[_SessionAccumulator]) -> ImplicitPreferenceProfile:
    """Convert a list of per-session accumulators into a final profile."""
    if not sessions:
        return ImplicitPreferenceProfile(preferences=[], sample_size=0)

    # Compute corpus-wide timestamps for stability checks.
    all_first = [s.first_ts for s in sessions if s.first_ts is not None]
    all_last = [s.last_ts for s in sessions if s.last_ts is not None]
    corpus_first_ts = min(all_first) if all_first else None
    corpus_last_ts = max(all_last) if all_last else None

    prefs: list[ImplicitPreference] = []

    # -----------------------------------------------------------------------
    # 1. Edit vs Write preference
    # -----------------------------------------------------------------------
    prefs.extend(_detect_edit_strategy(sessions, corpus_first_ts, corpus_last_ts))

    # -----------------------------------------------------------------------
    # 2. Shell command preferences
    # -----------------------------------------------------------------------
    prefs.extend(_detect_shell_commands(sessions, corpus_first_ts, corpus_last_ts))

    # -----------------------------------------------------------------------
    # 3. Format preference (emoji absence)
    # -----------------------------------------------------------------------
    prefs.extend(_detect_emoji_format(sessions, corpus_first_ts, corpus_last_ts))

    # -----------------------------------------------------------------------
    # 4. Vocabulary patterns
    # -----------------------------------------------------------------------
    prefs.extend(_detect_vocabulary(sessions, corpus_first_ts, corpus_last_ts))

    return ImplicitPreferenceProfile(
        preferences=prefs,
        sample_size=len(sessions),
    )


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------


def _detect_edit_strategy(
    sessions: list[_SessionAccumulator],
    first_ts: float | None,
    last_ts: float | None,
) -> list[ImplicitPreference]:
    """Emit edit_strategy preference when Edit dominates Write by ≥ 2× across sessions."""
    # Count sessions where Edit > Write (evidence) and sessions where Write > Edit (contradiction).
    edit_dominant_sessions: list[str] = []
    write_dominant_sessions: list[str] = []
    cwds: set[str] = set()

    for s in sessions:
        if s.edit_calls == 0 and s.write_calls == 0:
            continue
        cwds.add(s.cwd)
        total = s.edit_calls + s.write_calls
        if total == 0:
            continue
        if s.edit_calls > s.write_calls * _EDIT_WRITE_RATIO_THRESHOLD:
            edit_dominant_sessions.append(s.session_id)
        elif s.write_calls > s.edit_calls * _EDIT_WRITE_RATIO_THRESHOLD:
            write_dominant_sessions.append(s.session_id)

    result: list[ImplicitPreference] = []

    if edit_dominant_sessions:
        n_evidence = len(edit_dominant_sessions)
        n_contra = len(write_dominant_sessions)
        total = n_evidence + n_contra
        confidence = n_evidence / total if total > 0 else 0.0
        pref = ImplicitPreference(
            category="edit_strategy",
            value="prefer_edit",
            confidence=round(confidence, 3),
            evidence_sessions=n_evidence,
            evidence_projects=len(cwds),
            contradiction_count=n_contra,
            sample_phrases=[],
        )
        if pref.is_promoted(first_ts, last_ts):
            result.append(pref)

    return result


def _detect_shell_commands(
    sessions: list[_SessionAccumulator],
    first_ts: float | None,
    last_ts: float | None,
) -> list[ImplicitPreference]:
    """Detect dominant command-line tool preferences within functional groups."""
    # Aggregate per-group, per-command evidence across sessions.
    # group_index → {command → set of session_ids that used it}
    group_session_sets: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    group_cwds: dict[int, set[str]] = defaultdict(set)
    # Contradiction: a session that used more than one command in the same group.
    group_contradicting_sessions: dict[int, set[str]] = defaultdict(set)

    for s in sessions:
        # Map each command seen in this session to its group (if any).
        group_commands_this_session: dict[int, list[str]] = defaultdict(list)
        for cmd, _ in s.cmd_counts.items():
            gi = _CMD_TO_GROUP.get(cmd)
            if gi is not None:
                group_commands_this_session[gi].append(cmd)

        for gi, cmds in group_commands_this_session.items():
            group_cwds[gi].add(s.cwd)
            if len(cmds) > 1:
                # Session used multiple commands in the same group → contradiction.
                group_contradicting_sessions[gi].add(s.session_id)
            for cmd in cmds:
                group_session_sets[gi][cmd].add(s.session_id)

    result: list[ImplicitPreference] = []

    for gi, cmd_sessions in group_session_sets.items():
        # Total distinct sessions that used any command in this group.
        all_sessions: set[str] = set()
        for sess_set in cmd_sessions.values():
            all_sessions |= sess_set

        if len(all_sessions) < _CMD_GROUP_MIN_CALLS:
            continue

        # Find the dominant command.
        sorted_cmds = sorted(cmd_sessions.items(), key=lambda kv: len(kv[1]), reverse=True)
        winner_cmd, winner_sessions = sorted_cmds[0]
        dominance = len(winner_sessions) / len(all_sessions)

        if dominance < _CMD_GROUP_DOMINANCE_THRESHOLD:
            continue

        n_evidence = len(winner_sessions)
        n_contra = len(group_contradicting_sessions[gi])

        confidence = dominance
        pref = ImplicitPreference(
            category="shell_command",
            value=f"prefer_{winner_cmd}",
            confidence=round(confidence, 3),
            evidence_sessions=n_evidence,
            evidence_projects=len(group_cwds[gi]),
            contradiction_count=n_contra,
            sample_phrases=[],
        )
        if pref.is_promoted(first_ts, last_ts):
            result.append(pref)

    return result


def _detect_emoji_format(
    sessions: list[_SessionAccumulator],
    first_ts: float | None,
    last_ts: float | None,
) -> list[ImplicitPreference]:
    """Emit format=no_emojis_in_chat when emoji use is negligible."""
    total_natural = sum(s.natural_turns for s in sessions)
    total_emoji = sum(s.emoji_turns for s in sessions)

    if total_natural == 0:
        return []

    emoji_ratio = total_emoji / total_natural
    if emoji_ratio >= _EMOJI_ABSENT_THRESHOLD:
        return []

    # Evidence: sessions with at least one natural turn and zero emoji.
    evidence_sessions = [s for s in sessions if s.natural_turns > 0 and s.emoji_turns == 0]
    contra_sessions = [s for s in sessions if s.emoji_turns > 0]
    cwds = {s.cwd for s in sessions if s.natural_turns > 0}

    n_evidence = len(evidence_sessions)
    n_contra = len(contra_sessions)
    total = n_evidence + n_contra
    confidence = n_evidence / total if total > 0 else 0.0

    pref = ImplicitPreference(
        category="format",
        value="no_emojis_in_chat",
        confidence=round(confidence, 3),
        evidence_sessions=n_evidence,
        evidence_projects=len(cwds),
        contradiction_count=n_contra,
        sample_phrases=[],
    )
    if pref.is_promoted(first_ts, last_ts):
        return [pref]
    return []


def _detect_vocabulary(
    sessions: list[_SessionAccumulator],
    first_ts: float | None,
    last_ts: float | None,
) -> list[ImplicitPreference]:
    """Detect recurring operator phrases (bigrams/trigrams) as vocabulary preferences."""
    # Count: phrase → set of session_ids where it appeared.
    phrase_sessions: dict[str, set[str]] = defaultdict(set)
    phrase_cwds: dict[str, set[str]] = defaultdict(set)
    # Raw phrase → example session turn (for sample_phrases — not stored here; we
    # build sample_phrases from the phrase itself as a short self-citation).

    for s in sessions:
        # Use top ngrams from this session to avoid rare noise.
        for phrase, count in s.ngram_counts.most_common(50):
            if count >= 2:  # phrase must appear ≥2 times in the same session
                phrase_sessions[phrase].add(s.session_id)
                phrase_cwds[phrase].add(s.cwd)

    # Sort by session coverage descending, keep top-K for efficiency.
    ranked = sorted(phrase_sessions.items(), key=lambda kv: len(kv[1]), reverse=True)[:_VOCAB_TOP_K]

    result: list[ImplicitPreference] = []
    for phrase, sess_set in ranked:
        n_sessions = len(sess_set)
        if n_sessions < _VOCAB_MIN_SESSIONS:
            continue

        n_projects = len(phrase_cwds[phrase])
        # No "contradiction" concept for vocabulary — either you use the phrase or you don't.
        confidence = min(1.0, n_sessions / max(len(sessions), 1))
        pref = ImplicitPreference(
            category="vocabulary",
            value=phrase,
            confidence=round(confidence, 3),
            evidence_sessions=n_sessions,
            evidence_projects=n_projects,
            contradiction_count=0,
            sample_phrases=[phrase],  # the phrase itself is its own citation
        )
        if pref.is_promoted(first_ts, last_ts):
            result.append(pref)

    return result
