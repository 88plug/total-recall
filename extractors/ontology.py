"""Ontology extractor — projects, machines, vocabulary.

Walks the ``~/.claude/projects/<slug>/`` corpus to build the operator's
domain graph:

1. **Projects**: one row per ``<slug>`` directory. We deliberately store
   the *slug* in the ``cwd`` column rather than the decoded path —
   slug-decode is lossy (a directory name with ``-`` in it is
   indistinguishable from a path separator), so the slug is the only
   round-trip-safe identifier. Callers that need a display path can call
   :func:`lib.jsonl_walker.derive_cwd_from_slug` on the way out.

2. **Machines**: regex-mined hostnames + LAN / Tailscale / public IPs +
   GPU mentions, scoped to ``±200`` characters around the hostname hit so
   we don't pair a Tailscale IP with the wrong host when several appear in
   the same line.

3. **Vocabulary**: 100% learned from the operator's own corpus. No hardcoded
   seed. Terms are promoted when they appear frequently across multiple
   sessions AND are not common English or generic dev/Claude-Code vocabulary.
   Definitions start empty; rich authoring is done on-demand via
   ``define_term``. The persistence layer's ``upsert_vocabulary_term`` /
   ``bump_vocabulary_frequency`` are still the write contract.

Designed to be cheap: streaming line-by-line per JSONL, regex-only, no
embeddings, no NLP libs. One full corpus pass is the unit of work.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# English wordlist — loaded once, used by the vocabulary specificity filter.
# Mirrors the pattern in extractors/voice_profile.py.
# ---------------------------------------------------------------------------

_ENGLISH_WORDS_CACHE: frozenset[str] | None = None


def _load_english_wordlist() -> frozenset[str]:
    """Return a frozenset of lowercase English words.

    Tries /usr/share/dict/words first (Linux/macOS system wordlist, ~100k
    entries). Falls back to an embedded ~600-word common-English set that
    covers the vocabulary most likely to pollute operator-specific mining
    (common verbs, nouns, adjectives, pronouns).
    """
    system_path = "/usr/share/dict/words"
    try:
        with open(system_path, encoding="utf-8", errors="ignore") as fh:
            words = frozenset(w.strip().lower() for w in fh if w.strip().isalpha())
        if len(words) > 1000:
            return words
    except OSError:
        pass

    # Embedded fallback — intentionally broad to suppress common English noise.
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
        "port", "pull", "push", "put", "re", "read", "real", "remove",
        "restart", "right", "run", "same", "see", "server", "set", "should",
        "show", "since", "so", "some", "start", "still", "stop", "sure",
        "take", "test", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "through", "time", "to", "too",
        "true", "try", "two", "under", "up", "update", "use", "used",
        "using", "very", "via", "want", "was", "way", "we", "were", "what",
        "when", "where", "which", "while", "who", "why", "will", "with",
        "work", "write", "yes", "yet", "you", "your",
        "agent", "home", "user", "state", "read", "type", "item", "node",
        "root", "base", "info", "send", "load", "save", "mode", "view",
        "move", "step", "task", "line", "main", "plan", "repo", "role",
        "rule", "side", "size", "sort", "spec", "text", "tree", "type",
        "unit", "word", "note", "host", "link", "call", "cast", "bind",
        "done", "fail", "feel", "flag", "form", "gate", "give", "grab",
        "hard", "hash", "head", "hold", "hook", "idea", "init", "join",
        "kind", "lack", "mark", "mean", "meet", "miss", "name", "near",
        "need", "next", "nice", "null", "once", "pair", "pass", "ping",
        "pipe", "poll", "pool", "post", "prep", "rate", "rate", "wrap",
        "wait", "warn", "with", "sign", "skip", "slow", "snap", "span",
        "spin", "stat", "stem", "swap", "sync", "tail", "tell", "tend",
        "test", "tick", "trim", "turn", "vary", "void", "walk", "watch",
    )
    return frozenset(_COMMON)


def _get_english_words() -> frozenset[str]:
    global _ENGLISH_WORDS_CACHE
    if _ENGLISH_WORDS_CACHE is None:
        _ENGLISH_WORDS_CACHE = _load_english_wordlist()
    return _ENGLISH_WORDS_CACHE

log = logging.getLogger(__name__)

__all__ = [
    "ProjectRecord",
    "MachineRecord",
    "VocabularyTerm",
    "OntologySnapshot",
    "extract_ontology",
    "extract_ontology_from_records",
    "persist_ontology",
    "iter_project_dirs",
    "update_vocabulary_counts",
    "compute_co_mention_graph",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProjectRecord:
    cwd: str
    display_name: str = ""
    purpose: str = ""
    primary_language: str = ""
    related_projects: list = field(default_factory=list)
    first_seen_ts: int = 0
    last_active_ts: int = 0
    message_count: int = 0


@dataclass
class MachineRecord:
    hostname: str
    role: str = ""
    lan_ip: str = ""
    tailscale_ip: str = ""
    public_ip: str = ""
    gpu: str = ""
    os: str = ""
    notes: str = ""
    last_seen_ts: int = 0


@dataclass
class VocabularyTerm:
    term: str
    definition: str
    category: str = "concept"
    frequency: int = 1
    first_seen_ts: int = 0


@dataclass
class OntologySnapshot:
    projects: list[ProjectRecord] = field(default_factory=list)
    machines: list[MachineRecord] = field(default_factory=list)
    vocabulary: list[VocabularyTerm] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data-driven vocabulary miner
#
# No hardcoded seed. Terms are mined from the operator's own corpus and
# promoted only when they meet cross-session corroboration + frequency
# thresholds. This ensures the vocabulary table reflects whoever the
# operator actually is, not the plugin author.
#
# Design choices:
# - Token extraction: simple word-boundary tokenisation + 2-gram scanning
#   over operator turns only (user-role messages). We skip assistant turns
#   because those are Claude-generated, not operator vocabulary.
# - Filtering: two blocklists remove noise before any counting:
#     _STOPWORDS  — common English words
#     _GENERIC_TECH  — universal dev/Claude-Code terms every installer has
# - Promotion thresholds (tunable via module constants below):
#     VOCAB_MIN_SESSIONS  — must appear in at least this many distinct sessions
#     VOCAB_MIN_FREQ  — must appear this many times across all sessions
# - Context snippets: the first occurrence's surrounding 60 chars are kept
#   as a lightweight "definition starter". Rich definitions are left to the
#   on-demand ``define_term`` path.
#
# Extension point: to add context-based definition mining (e.g. extracting
# the sentence that first defines a term), replace the ``_snippet_for_term``
# stub below with a heavier extractor. The rest of the pipeline is unchanged.
# ---------------------------------------------------------------------------

# Promotion thresholds — adjust if too noisy or too sparse.
VOCAB_MIN_SESSIONS: int = 2   # must appear in at least N distinct sessions
VOCAB_MIN_FREQ: int = 4       # must appear at least N times total

# Generic Claude-Code product vocabulary that every installer shares.
# We keep these so ``define_term`` can explain them, but they are NOT
# operator-specific and are pre-populated as universal concepts rather
# than mined. None of these are author-private.
_UNIVERSAL_CLAUDE_CODE_TERMS: list[VocabularyTerm] = [
    VocabularyTerm(
        "cwd-slug",
        "Filesystem slug derived from cwd by replacing / with -, "
        "used as ~/.claude/projects/<slug>/ dir name.",
        "concept",
    ),
    VocabularyTerm(
        "session DAG",
        "Sessions are DAGs via parentUuid, not flat lists — branches and sidechains exist.",
        "concept",
    ),
    VocabularyTerm(
        "sidechain",
        "isSidechain=true records — subagent runs that should fold under their parent turn.",
        "concept",
    ),
    VocabularyTerm(
        "compaction",
        "Mid-session context reduction event recorded as system.subtype=compact_boundary.",
        "concept",
    ),
]

# Common English stopwords — single-word only (2-grams checked separately).
# Also includes generic path-segment noise (home, user, name, state, read,
# agent, …) that leaks through the length filter but is never operator-specific.
_STOPWORDS: frozenset[str] = frozenset(
    [
        'a', 'about', 'above', 'after', 'again', 'against', 'all', 'also', 'am', 'an', 'and',
        'any', 'are', "aren't", 'as', 'at', 'be', 'because', 'been', 'before', 'being',
        'below', 'between', 'both', 'but', 'by', 'can', "can't", 'cannot', 'could', "couldn't",
        'did', "didn't", 'do', 'does', "doesn't", 'doing', "don't", 'down', 'during', 'each',
        'few', 'for', 'from', 'further', 'get', 'go', 'goes', 'going', 'got', 'had', "hadn't",
        'has', "hasn't", 'have', "haven't", 'having', 'he', "he'd", "he'll", "he's", 'her',
        'here', "here's", 'hers', 'herself', 'him', 'himself', 'his', 'how', "how's", 'i',
        "i'd", "i'll", "i'm", "i've", 'if', 'in', 'into', 'is', "isn't", 'it', "it's", 'its',
        'itself', 'just', "let's", 'me', 'more', 'most', "mustn't", 'my', 'myself', 'no',
        'nor', 'not', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'ought', 'our', 'ours',
        'ourselves', 'out', 'over', 'own', 'same', "shan't", 'she', "she'd", "she'll", "she's",
        'should', "shouldn't", 'so', 'some', 'such', 'than', 'that', "that's", 'the', 'their',
        'theirs', 'them', 'themselves', 'then', 'there', "there's", 'these', 'they', "they'd",
        "they'll", "they're", "they've", 'this', 'those', 'through', 'to', 'too', 'under',
        'until', 'up', 'very', 'was', "wasn't", 'we', "we'd", "we'll", "we're", "we've",
        'were', "weren't", 'what', "what's", 'when', "when's", 'where', "where's", 'which',
        'while', 'who', "who's", 'whom', 'why', "why's", 'will', 'with', "won't", 'would',
        "wouldn't", 'you', "you'd", "you'll", "you're", "you've", 'your', 'yours', 'yourself',
        'yourselves', 'also', 'already', 'always', 'another', 'back', 'been', 'being', 'best',
        'better', 'both', 'call', 'called', 'check', 'come', 'comes', 'create', 'done', 'each',
        'else', 'end', 'even', 'every', 'example', 'first', 'following', 'found', 'get',
        'given', 'good', 'here', 'however', 'instead', 'just', 'keep', 'last', 'like', 'look',
        'made', 'make', 'many', 'may', 'maybe', 'might', 'much', 'must', 'need', 'never',
        'new', 'next', 'non', 'now', 'old', 'one', 'other', 'our', 'own', 'part', 'please',
        'put', 'really', 'right', 'run', 'same', 'see', 'set', 'show', 'since', 'still',
        'take', 'then', 'there', 'thing', 'things', 'think', 'though', 'time', 'too', 'true',
        'until', 'upon', 'use', 'used', 'using', 'via', 'want', 'way', 'well', 'where',
        'while', 'without', 'yet', 'home', 'name', 'state', 'read', 'user', 'file', 'path',
        'type', 'item', 'node', 'root', 'base', 'info', 'send', 'load', 'save', 'mode', 'view',
        'step', 'task', 'line', 'main', 'plan', 'repo', 'role', 'rule', 'side', 'size', 'sort',
        'spec', 'text', 'tree', 'unit', 'word', 'note', 'host', 'link', 'cast', 'bind', 'feel',
        'flag', 'form', 'gate', 'give', 'grab', 'hard', 'hash', 'head', 'hold', 'hook', 'idea',
        'init', 'join', 'kind', 'lack', 'mark', 'mean', 'meet', 'miss', 'near', 'need', 'nice',
        'null', 'once', 'pair', 'pass', 'ping', 'pipe', 'poll', 'pool', 'post', 'prep', 'rate',
        'wrap', 'wait', 'warn', 'sign', 'skip', 'slow', 'snap', 'span', 'spin', 'stat', 'stem',
        'swap', 'sync', 'tail', 'tell', 'tend', 'tick', 'trim', 'turn', 'vary', 'void', 'walk',
        'watch', 'agent', 'open', 'data', 'code', 'work', 'test', 'true', 'real', 'done',
        'fail', 'help', 'push', 'pull', 'keep', 'next', 'make', 'move', 'show', 'stop',
        'start', 'some', 'more', 'find', 'just', 'over', 'back', 'into', 'this', 'from',
        'also',
    ]
)

# Generic dev / Claude Code terms that appear for ALL operators — not worth
# mining as operator-specific vocabulary.
_GENERIC_TECH: frozenset[str] = frozenset(
    [
        'git', 'github', 'gitlab', 'branch', 'commit', 'push', 'pull', 'merge', 'rebase',
        'diff', 'patch', 'tag', 'branch', 'checkout', 'clone', 'fetch', 'stash', 'cherry-pick',
        'bisect', 'worktree', 'python', 'javascript', 'typescript', 'node', 'npm', 'yarn',
        'pnpm', 'pip', 'uv', 'virtualenv', 'venv', 'docker', 'container', 'image', 'registry',
        'volume', 'mount', 'bind', 'bash', 'shell', 'sh', 'zsh', 'fish', 'script', 'curl',
        'wget', 'grep', 'find', 'sed', 'awk', 'jq', 'json', 'yaml', 'toml', 'ini', 'xml',
        'html', 'css', 'sql', 'sqlite', 'postgres', 'mysql', 'redis', 'function', 'class',
        'method', 'import', 'export', 'module', 'package', 'library', 'test', 'pytest',
        'unittest', 'jest', 'mocha', 'spec', 'mock', 'fixture', 'assert', 'api', 'rest',
        'graphql', 'grpc', 'http', 'https', 'webhook', 'endpoint', 'route', 'handler',
        'config', 'env', 'variable', 'secret', 'token', 'key', 'value', 'flag', 'option',
        'argument', 'error', 'exception', 'warning', 'debug', 'log', 'trace', 'stdout',
        'stderr', 'file', 'path', 'directory', 'folder', 'symlink', 'stat', 'chmod', 'chown',
        'process', 'thread', 'async', 'await', 'promise', 'callback', 'event', 'loop', 'queue',
        'server', 'client', 'request', 'response', 'status', 'code', 'header', 'body', 'index',
        'schema', 'table', 'column', 'row', 'query', 'insert', 'update', 'delete', 'select',
        'claude', 'anthropic', 'mcp', 'tool', 'skill', 'hook', 'plugin', 'command', 'slash',
        'session', 'transcript', 'jsonl', 'record', 'turn', 'message', 'content', 'assistant',
        'user', 'context', 'window', 'token', 'prompt', 'completion', 'inference', 'model',
        'readme', 'changelog', 'license', 'makefile', 'dockerfile', 'pyproject', 'toml', 'pr',
        'pull', 'request', 'review', 'comment', 'issue', 'milestone', 'label', 'ci', 'cd',
        'pipeline', 'stage', 'job', 'artifact', 'deploy', 'release', 'version', 'tag',
    ]
)

_DEFAULT_PROJECTS_ROOT = Path("~/.claude/projects").expanduser()

# Minimum token length to consider for vocabulary mining.
_VOCAB_MIN_TOKEN_LEN: int = 4

# Regex to extract candidate tokens from operator text (words + hyphenated compounds).
_TOKEN_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9]*(?:-[a-zA-Z0-9]+)*)\b")

# Minimum length for a bare-alpha word to be considered operator-specific when
# it is NOT in the English wordlist (e.g. "opnsense", "wireguard").
_BARE_ALPHA_MIN_LEN: int = 5


def _derive_operator_usernames(projects_root: Path) -> frozenset[str]:
    """Detect the operator's Unix username(s) from /home/<name>/ path segments.

    Scans the cwd slug of each project directory (format: -home-<name>-...).
    Returns the set of all <name> values found — typically just one.
    Purely data-driven; no hardcoded names.
    """
    names: set[str] = set()
    if not projects_root.is_dir():
        return frozenset()
    for child in projects_root.iterdir():
        if not child.is_dir():
            continue
        # Slug: -home-<username>-...  Strip leading dash then split.
        parts = child.name.lstrip("-").split("-")
        # Look for the "home" segment and take the following segment as username.
        for i, part in enumerate(parts):
            if part == "home" and i + 1 < len(parts):
                candidate = parts[i + 1]
                if candidate and candidate.isalpha() and len(candidate) >= 2:
                    names.add(candidate.lower())
    return frozenset(names)


# Module-level cache so we only derive once per process invocation.
_OPERATOR_USERNAMES: frozenset[str] | None = None


def _get_operator_usernames(projects_root: Path) -> frozenset[str]:
    global _OPERATOR_USERNAMES
    if _OPERATOR_USERNAMES is None:
        _OPERATOR_USERNAMES = _derive_operator_usernames(projects_root)
    return _OPERATOR_USERNAMES


def _snippet_for_term(term: str, text: str, window: int = 60) -> str:
    """Return a short context snippet around the first occurrence of ``term``.

    Lightweight definition starter — the first sentence/phrase mentioning
    the term. Rich definition authoring is left to the ``define_term``
    on-demand path.

    Extension point: replace this function with a heavier context-based
    extractor (e.g. sentence-boundary aware extraction, or co-occurrence
    clustering) without changing any other code.
    """
    idx = text.lower().find(term.lower())
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(term) + window)
    snippet = text[start:end].strip()
    # Trim to nearest sentence boundary if possible.
    for sep in (".", "!", "?", "\n"):
        cut = snippet.find(sep, window)
        if 0 < cut < len(snippet):
            snippet = snippet[: cut + 1]
            break
    return snippet[:120]


def _has_specificity_marker(tok: str) -> bool:
    """Return True if ``tok`` carries a structural marker that makes it
    operator-specific regardless of whether it appears in a wordlist.

    Specificity markers (any one is sufficient):
    - Contains a hyphen  → compound service/hostname style (``relay-eu-west``)
    - Contains a digit   → version/ID-suffixed name (``racknerd-4b4fa33``)
    - Contains a dot     → FQDN/namespace style (``api.example.com``)
    - Is mixed-case/camelCase → proper noun or acronym (``WireGuard``, ``OpnSense``)
    - Is a multi-word phrase  → already operator-specific by definition
    - Is a bare alpha word NOT in the English wordlist AND length >= _BARE_ALPHA_MIN_LEN
      → coined / domain name (``opnsense``, ``wireguard``, ``litestream``)
    """
    # Structural markers — fast O(1) checks first.
    if "-" in tok:
        return True
    if any(c.isdigit() for c in tok):
        return True
    if "." in tok:
        return True
    if " " in tok:
        return True

    # Mixed-case / camelCase: has both upper and lower letters and the first
    # uppercase is NOT just the leading character (e.g. "WireGuard", not "Wire").
    lower = tok.lower()
    has_upper_after_first = any(c.isupper() for c in tok[1:])
    has_lower = any(c.islower() for c in tok)
    if has_upper_after_first and has_lower:
        return True

    # Bare alpha word: keep only if long enough AND not a known English word.
    if tok.isalpha() and len(lower) >= _BARE_ALPHA_MIN_LEN:
        english_words = _get_english_words()
        if lower not in english_words:
            return True

    return False


# Exact-token blocklist for session-mechanics / harness artifacts.
# These contain hyphens so they pass the specificity gate, but they are
# never operator vocabulary — they come from Claude Code harness scaffolding
# (task-notification blocks, tool call IDs, tmp dirs, ls output, cwd slugs).
_HARNESS_ARTIFACT_EXACT: frozenset[str] = frozenset(
    [
        'task-notification', 'task-id', 'tool-use-id', 'output-file', 'rw-rw-r', 'rw-rw-rw',
        'drwxr-xr', 'drwxrwxr',
    ]
)

# Regex patterns for harness/path artifact shapes.
# Checked against the lowercase token.
_HARNESS_ARTIFACT_RE = re.compile(
    r"^toolu[_-]"          # tool-call IDs: toolu_abc123 / toolu-abc123
    r"|^claude-\d"         # tmp dirs: claude-1000, claude-2345
    r"|^home-[a-z]"        # cwd slug prefix: home-dana-myproj  (not a vocab term)
    r"|^[dlcbps-][rwx-]{9}$"  # unix permission strings: drwxr-xr-x
    r"|^\d+[kmgt]b?$",     # file size tokens: 42kb, 100mb, 4gb
    re.IGNORECASE,
)


def _is_harness_artifact(tok: str) -> bool:
    """Return True if ``tok`` is a Claude Code session-mechanics artifact.

    These tokens contain hyphens or digits so they pass the specificity gate,
    but they originate from harness scaffolding (tool-call IDs, task blocks,
    tmp dir names, cwd slugs, ls output), never from the operator's domain
    vocabulary.  Called before promotion to prevent them from polluting the
    vocabulary table.
    """
    lower = tok.lower()
    if lower in _HARNESS_ARTIFACT_EXACT:
        return True
    return bool(_HARNESS_ARTIFACT_RE.search(lower))


def _is_boring_token(tok: str, operator_usernames: frozenset[str] | None = None) -> bool:
    """True if ``tok`` is a stopword, generic tech term, too short, or
    lacks any specificity marker indicating it is operator-specific."""
    lower = tok.lower()
    if len(lower) < _VOCAB_MIN_TOKEN_LEN:
        return True
    if lower in _STOPWORDS:
        return True
    if lower in _GENERIC_TECH:
        return True
    # Pure numeric or version strings (e.g. "1234", "v0.3") — skip.
    if re.fullmatch(r"v?\d+[\d.]*", lower):
        return True
    # Drop the operator's own username — it appears everywhere but is not vocab.
    if operator_usernames and lower in operator_usernames:
        return True
    # Drop harness/tooling/path artifacts even though they contain hyphens
    # (which would otherwise pass the specificity gate below).
    if _is_harness_artifact(tok):
        return True
    # Specificity filter: drop generic English words that have no structural
    # marker making them operator-specific.
    return bool(not _has_specificity_marker(tok))


def _extract_operator_tokens(
    rec: dict,
    operator_usernames: frozenset[str] | None = None,
) -> list[str]:
    """Extract candidate vocabulary tokens from a user-role record only.

    We mine operator turns exclusively — assistant turns are Claude output,
    not operator vocabulary.
    """
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    if msg.get("role") != "user":
        return []
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content")
                if isinstance(t, str):
                    parts.append(t)
    if not parts:
        return []
    text = " ".join(parts)
    tokens = _TOKEN_RE.findall(text)
    return [t for t in tokens if not _is_boring_token(t, operator_usernames)]


def mine_vocabulary(
    projects_root: Path,
    min_sessions: int = VOCAB_MIN_SESSIONS,
    min_freq: int = VOCAB_MIN_FREQ,
) -> list[VocabularyTerm]:
    """Data-driven vocabulary miner.

    Scans the full corpus. For each token that appears in at least
    ``min_sessions`` distinct sessions with total count >= ``min_freq``,
    emit a :class:`VocabularyTerm` with an auto-extracted context snippet
    as a provisional definition.

    Returns terms sorted by frequency descending.
    """
    # Derive the operator's username(s) from the project slugs so we can
    # drop them from vocabulary (they appear everywhere but are not vocab).
    operator_usernames = _derive_operator_usernames(projects_root)

    # term → {session_ids}
    term_sessions: dict[str, set[str]] = defaultdict(set)
    # term → total count
    term_counts: Counter[str] = Counter()
    # term → first snippet seen
    term_snippet: dict[str, str] = {}
    # term → first_seen_ts
    term_first_ts: dict[str, int] = {}

    for proj_dir in iter_project_dirs(projects_root):
        for session_file in _iter_session_files(proj_dir):
            session_id = session_file.stem  # UUID
            try:
                file_ts = int(session_file.stat().st_mtime)
            except OSError:
                file_ts = 0

            for rec in _iter_jsonl_records(session_file):
                tokens = _extract_operator_tokens(rec, operator_usernames)
                if not tokens:
                    continue
                # For snippet extraction, rebuild the full text from this rec.
                rec_text = _flatten_record_text(rec) if term_snippet is not None else ""
                for tok in tokens:
                    key = tok.lower()
                    term_sessions[key].add(session_id)
                    term_counts[key] += 1
                    if key not in term_snippet and rec_text:
                        snip = _snippet_for_term(tok, rec_text)
                        if snip:
                            term_snippet[key] = snip
                    if key not in term_first_ts:
                        term_first_ts[key] = file_ts

    promoted: list[VocabularyTerm] = []
    for key, sessions in term_sessions.items():
        if len(sessions) < min_sessions:
            continue
        freq = term_counts[key]
        if freq < min_freq:
            continue
        # Use original-case from first occurrence if we can recover it,
        # otherwise fall back to lowercase key.
        definition = term_snippet.get(key, "")
        promoted.append(
            VocabularyTerm(
                term=key,
                definition=definition,
                category="concept",
                frequency=freq,
                first_seen_ts=term_first_ts.get(key, 0),
            )
        )

    promoted.sort(key=lambda v: v.frequency, reverse=True)
    return promoted


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Hostnames — generic detection.  We look for identifiers that appear in
# deployment/networking contexts: ssh targets, deploy references, hostnames
# mentioned alongside IPs.  Pattern: label-style names (letters/digits/hyphens,
# no path separators) that appear in at least one of those contexts.  We do NOT
# hardcode specific names.
#
# The regex below matches multi-segment hostnames (e.g. ``server-01``,
# ``relay-eu-1``, ``db.prod.example.com``) and is intentionally broad;
# the surrounding-context role classifier and cross-session frequency
# filtering do the real narrowing.
_HOSTNAME_RE = re.compile(
    r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+(?:\.[a-z][a-z0-9]+)*|"  # hyphenated hostname
    r"[a-z][a-z0-9]*\.[a-z][a-z0-9]+\.[a-z]{2,})\b",           # dotted FQDN
    re.IGNORECASE,
)

# All RFC1918 private ranges + loopback.
_LAN_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)
_TAILSCALE_RE = re.compile(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_PUBLIC_IP_RE = re.compile(r"\b(?:(?:\d{1,3}\.){3}\d{1,3})\b")
_GPU_RE = re.compile(r"\b(RTX|GTX)\s?(\d{3,4})\s?(Ti|Super)?\b", re.IGNORECASE)
_GPU_VRAM_RE = re.compile(r"\b(\d{1,3})\s?Gi?B\b", re.IGNORECASE)

# Role-classification heuristic. Searched within ±200 chars of the hostname.
_PROXMOX_RE = re.compile(r"\bproxmox\b", re.IGNORECASE)
_RELAY_RE = re.compile(r"\brelay\b", re.IGNORECASE)
_WORKSTATION_RE = re.compile(r"\b(workstation|laptop|desktop)\b", re.IGNORECASE)

# Generic data-center / cloud region keywords — provider-agnostic.
# The provider label (OVH, AWS, Hetzner, etc.) is derived at query time
# from ``standing_decisions`` if available, not hardcoded here.
_REGION_RE = re.compile(
    r"\b(paris|bhs|warsaw|singapore|sydney|gravelines|roubaix|strasbourg"
    r"|us-east|us-west|eu-west|ap-southeast|ap-northeast"
    r"|nyc|lon|fra|ams|sgp|sfo|tor|blr)\b",
    re.IGNORECASE,
)

# Language-frequency probe — file-extension references in user/assistant text.
_LANG_EXT_RE = re.compile(r"\.(go|py|ts|tsx|js|jsx|rs|sh|md|sql)\b", re.IGNORECASE)
_EXT_TO_LANG = {
    "go": "go",
    "py": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "rs": "rust",
    "sh": "shell",
    "md": "markdown",
    "sql": "sql",
}

_CONTEXT_WINDOW = 200


# ---------------------------------------------------------------------------
# Corpus iteration helpers
# ---------------------------------------------------------------------------


def iter_project_dirs(
    projects_root: Path = _DEFAULT_PROJECTS_ROOT,
) -> Iterator[Path]:
    """Yield each ``<slug>`` directory under ``projects_root``.

    Skips files and dot-dirs. Sorted lexicographically for stable test output.
    """
    if not projects_root.is_dir():
        return
    for child in sorted(projects_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield child


def _iter_session_files(proj_dir: Path) -> Iterator[Path]:
    for p in sorted(proj_dir.iterdir()):
        if p.is_file() and p.suffix == ".jsonl" and len(p.stem) >= 32:
            yield p


def _iter_jsonl_records(path: Path) -> Iterator[dict]:
    """Yield parsed records, silently skipping blank / malformed lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:  # pragma: no cover — filesystem race
        log.debug("could not read %s: %s", path, exc)
        return


def _flatten_record_text(rec: dict) -> str:
    """Pull every string-typed text field out of a record into one blob."""
    parts: list[str] = []
    msg = rec.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    t = blk.get("text") or blk.get("content")
                    if isinstance(t, str):
                        parts.append(t)
    for key in ("content", "text", "title"):
        v = rec.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-project extraction
# ---------------------------------------------------------------------------


def _project_record_for_dir(proj_dir: Path) -> ProjectRecord | None:
    """Build a single ``ProjectRecord`` from one project directory.

    ``cwd`` is the slug name (the directory's basename) — see module
    docstring for why we don't slug-decode.
    """
    sessions = list(_iter_session_files(proj_dir))
    if not sessions:
        return None

    cwd_slug = proj_dir.name

    # mtime range across all sessions.
    mtimes: list[float] = []
    for s in sessions:
        try:
            mtimes.append(s.stat().st_mtime)
        except OSError:
            continue
    first_ts = int(min(mtimes)) if mtimes else 0
    last_ts = int(max(mtimes)) if mtimes else 0

    # Single pass per session: count messages, count language extensions, and
    # grab the first user turn for `purpose`.
    lang_counts: Counter[str] = Counter()
    message_count = 0
    purpose = ""
    # Process sessions in mtime order so "earliest" = real chronological earliest.
    sessions_by_mtime = sorted(
        sessions, key=lambda p: p.stat().st_mtime if p.exists() else 0
    )
    for s in sessions_by_mtime:
        for rec in _iter_jsonl_records(s):
            t = rec.get("type")
            # message_count: count assistant + user records (the dense signal),
            # excluding pure-metadata records (ai-title, last-prompt, etc.).
            if t in ("assistant", "user"):
                message_count += 1
            text = _flatten_record_text(rec)
            if not text:
                continue
            for ext_match in _LANG_EXT_RE.findall(text):
                lang = _EXT_TO_LANG.get(ext_match.lower())
                if lang:
                    lang_counts[lang] += 1
            # Capture first user turn (earliest session, first user message)
            # as a short 'purpose' summary — clipped to 200 chars.
            if not purpose and t == "user":
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    raw = msg.get("content")
                    candidate = ""
                    if isinstance(raw, str):
                        candidate = raw
                    elif isinstance(raw, list):
                        for blk in raw:
                            if isinstance(blk, dict):
                                txt = blk.get("text")
                                if isinstance(txt, str):
                                    candidate = txt
                                    break
                    candidate = (candidate or "").strip()
                    # Skip slash-commands / tool-result tags / very short stubs.
                    if (
                        candidate
                        and not candidate.startswith("<")
                        and not candidate.startswith("/")
                        and len(candidate) >= 4
                    ):
                        # First line only, capped.
                        purpose = candidate.splitlines()[0][:200]

    primary_language = ""
    if lang_counts:
        primary_language = lang_counts.most_common(1)[0][0]

    return ProjectRecord(
        cwd=cwd_slug,
        display_name=cwd_slug,
        purpose=purpose,
        primary_language=primary_language,
        related_projects=[],
        first_seen_ts=first_ts,
        last_active_ts=last_ts,
        message_count=message_count,
    )


# ---------------------------------------------------------------------------
# Machine extraction
# ---------------------------------------------------------------------------


def _classify_role(window: str, hostname: str = "") -> str:
    """Best-effort role label from the text surrounding a hostname hit.

    Hostname-shape is the strongest signal — a multi-segment name starting
    with ``relay`` is a relay even if "Proxmox" appears nearby.  We check
    shape first, then fall back to keyword-in-window heuristics.

    Provider labels (e.g. "OVH") are intentionally absent here — derive
    them from ``standing_decisions`` at query time rather than hardcoding.
    """
    hn = hostname.lower()
    # Shape-based: relay-* style names.
    if hn.startswith("relay-") or hn.startswith("relay."):
        region = _REGION_RE.search(window)
        if region:
            return f"relay ({region.group(0).lower()})"
        return "relay"
    if _PROXMOX_RE.search(window):
        return "Proxmox host"
    region = _REGION_RE.search(window)
    if region:
        return f"relay ({region.group(0).lower()})"
    if _RELAY_RE.search(window):
        return "relay"
    if _WORKSTATION_RE.search(window):
        return "workstation"
    return ""


def _is_likely_public_ip(ip: str) -> bool:
    """True if ``ip`` is *not* in the private/Tailscale ranges we already mine."""
    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("100."):
        return False
    if ip.startswith("127.") or ip.startswith("0."):
        return False
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
        except (ValueError, IndexError):
            return True
        if 16 <= second <= 31:
            return False
    return True


# Plain-English / path-prose words that betray a cwd directory-slug rather than
# a real hostname. A hyphenated token containing any of these (or one that is
# long / many-segmented) is almost certainly a project slug
# (``a-conversation-with-daniel-kahneman-about-noise``,
# ``claude-code-session-logs-data-mining``) — NOT a machine. Real hosts use
# terse role/region/index segments (relay, eu, west, prod, db, 01), not prose.
# v0.14.0 fix for the ~61k garbage ``machines`` rows.
_SLUG_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "for", "with", "about",
        "from", "into", "this", "that", "these", "those", "is", "are", "be",
        "conversation", "session", "sessions", "logs", "log", "data", "mining",
        "code", "project", "projects", "notes", "draft", "review", "analysis",
        "summary", "report", "guide", "plan", "how", "what", "why", "when",
        "claude", "chat", "thread", "transcript", "doc", "docs", "readme",
        # Universal filesystem path roots — a hostname never starts with these.
        "home", "usr", "tmp", "var", "etc", "opt", "mnt", "srv", "root",
    }
)


def _is_cwd_slug(token: str) -> bool:
    """True if *token* looks like a cwd directory slug, not a hostname.

    A real hostname is terse. A directory slug is long, has many hyphen
    segments, or contains plain-English / path-prose words. Used to keep cwd
    path components out of the ``machines`` table.
    """
    f = token.strip().strip(",.;:!?()[]{}\"'")
    if len(f) > 30:
        return True
    segments = f.replace(".", "-").split("-")
    if len(segments) >= 5:
        return True
    return any(seg.lower() in _SLUG_WORDS for seg in segments)


_HEX_RUN_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)


def _looks_like_hex_id(token: str) -> bool:
    """True if *token* looks like a UUID / hash / hex-id fragment misdetected as
    a hostname (e.g. ``a013-4f1a-9a75-13c768f26f92``, ``a01d-5333afd7bc86``,
    ``a025ca8d7a59c5e9-iad``). The broad ``_HOSTNAME_RE`` matches these, and
    ``_is_cwd_slug`` (cwd-slug only) misses them — they were the dominant
    ``machines`` table garbage. Real hostnames use word/role/region/index
    segments, never long pure-hex runs.
    """
    f = token.strip().strip(",.;:!?()[]{}\"'")
    segs = f.replace(".", "-").split("-")
    if any(_HEX_RUN_RE.match(seg) for seg in segs):
        return True
    return bool(
        len(segs) >= 3
        and all(
            re.fullmatch(r"[0-9a-f]+", seg, re.IGNORECASE)
            and any(c.isdigit() for c in seg)
            for seg in segs
        )
    )


def _extract_machines_from_text(
    text: str, ts: int, machines: dict[str, MachineRecord]
) -> None:
    """Mutate ``machines`` in place with hits from a single text blob."""
    for m in _HOSTNAME_RE.finditer(text):
        host = m.group(0)
        # Canonicalise hostname casing for keys (lower-case).
        key = host.lower()
        # Skip cwd directory-slugs misdetected as hostnames (v0.14.0).
        if _is_cwd_slug(key):
            continue
        # Skip UUID / hash / hex-id fragments misdetected as hostnames.
        if _looks_like_hex_id(key):
            continue
        start = max(0, m.start() - _CONTEXT_WINDOW)
        end = min(len(text), m.end() + _CONTEXT_WINDOW)
        window = text[start:end]

        rec = machines.setdefault(key, MachineRecord(hostname=key))
        if ts > rec.last_seen_ts:
            rec.last_seen_ts = ts

        if not rec.role:
            rec.role = _classify_role(window, hostname=key)

        if not rec.lan_ip:
            lan = _LAN_RE.search(window)
            if lan:
                rec.lan_ip = lan.group(0)
        if not rec.tailscale_ip:
            ts_ip = _TAILSCALE_RE.search(window)
            if ts_ip:
                rec.tailscale_ip = ts_ip.group(0)
        if not rec.public_ip:
            for ip_m in _PUBLIC_IP_RE.finditer(window):
                cand = ip_m.group(0)
                if _is_likely_public_ip(cand):
                    rec.public_ip = cand
                    break
        if not rec.gpu:
            gpu = _GPU_RE.search(window)
            if gpu:
                rec.gpu = " ".join(p for p in gpu.groups() if p).strip()
                vram = _GPU_VRAM_RE.search(window)
                if vram:
                    rec.gpu = f"{rec.gpu} {vram.group(0)}"


# ---------------------------------------------------------------------------
# Top-level extractor
# ---------------------------------------------------------------------------


def extract_ontology(
    projects_root: Path = _DEFAULT_PROJECTS_ROOT,
) -> OntologySnapshot:
    """Run a full ontology pass over ``projects_root``.

    Returns an :class:`OntologySnapshot` with populated ``projects``,
    ``machines``, ``vocabulary``. Cheap enough for the regular extraction
    pipeline — single streaming pass, regex-only, no embeddings.
    """
    snap = OntologySnapshot()
    machines: dict[str, MachineRecord] = {}

    for proj_dir in iter_project_dirs(projects_root):
        proj = _project_record_for_dir(proj_dir)
        if proj is not None:
            snap.projects.append(proj)

        # Second walk (cheap re-iteration) so we can mine machines
        # without coupling them to the project-record state-machine.
        for s in _iter_session_files(proj_dir):
            try:
                ts = int(s.stat().st_mtime)
            except OSError:
                ts = 0
            for rec in _iter_jsonl_records(s):
                text = _flatten_record_text(rec)
                if not text:
                    continue
                _extract_machines_from_text(text, ts, machines)

    snap.machines = sorted(machines.values(), key=lambda m: m.hostname)

    # Vocabulary: universal Claude-Code terms + data-mined operator terms.
    # Universal terms are always present (generic, non-private).
    # Operator terms are mined from the corpus and promoted only when they
    # meet the cross-session + frequency thresholds.
    now = max(
        (p.last_active_ts for p in snap.projects), default=0
    ) or 0

    seen_terms: set[str] = set()
    for v in _UNIVERSAL_CLAUDE_CODE_TERMS:
        snap.vocabulary.append(
            VocabularyTerm(
                term=v.term,
                definition=v.definition,
                category=v.category,
                frequency=1,
                first_seen_ts=now,
            )
        )
        seen_terms.add(v.term.lower())

    for v in mine_vocabulary(projects_root):
        if v.term.lower() not in seen_terms:
            snap.vocabulary.append(v)
            seen_terms.add(v.term.lower())

    # Compute co-mention graph: scan session message text for cross-project
    # name references and populate each project's related_projects list.
    session_texts = _collect_session_texts(projects_root, snap.projects)
    co_mentions = compute_co_mention_graph(snap.projects, session_texts)
    for proj in snap.projects:
        proj.related_projects = co_mentions.get(proj.cwd, [])

    return snap


# ---------------------------------------------------------------------------
# Co-mention graph
# ---------------------------------------------------------------------------

# Names shorter than this are too generic to match reliably.
_CO_MENTION_MIN_NAME_LEN: int = 4

# Top-N related projects to keep per project.
_CO_MENTION_TOP_N: int = 5

# Common path-segment stopwords and generic names that should never be
# treated as project identifiers.  These are structural artifacts of the
# filesystem layout, not project names.  No operator-specific literals.
_CO_MENTION_STOPWORDS: frozenset[str] = frozenset(
    [
        'home', 'tmp', 'src', 'code', 'github', 'gitlab', 'users', 'opt', 'local', 'share',
        'bin', 'lib', 'projects', 'project', 'work', 'workspace', 'dev', 'repos', 'repo',
        'user',
    ]
)


def _name_is_generic(name: str) -> bool:
    """Return True if ``name`` is too short or a known-generic path segment.

    Guards against high-false-positive matches from names like "tmp", "src",
    "foo", "code", "dev" that would appear in almost any project's text.
    """
    low = name.lower().strip()
    if len(low) < _CO_MENTION_MIN_NAME_LEN:
        return True
    # Purely numeric strings are never project names.
    if low.isdigit():
        return True
    # Whole-word check against the combined stopword sets.
    return bool(low in _CO_MENTION_STOPWORDS or low in _STOPWORDS or low in _GENERIC_TECH)


def _candidate_names_for_project(proj: ProjectRecord) -> list[str]:
    """Derive non-generic candidate names for a project from its slug.

    We derive names purely from the slug (filesystem artifact), not from any
    hardcoded data.  The slug is the directory basename under
    ``~/.claude/projects/``.  It encodes the cwd with ``/`` → ``-``.

    Strategy: build suffix names of increasing length (1 to 3 right-most
    segments) and keep every suffix that is non-generic.  Having multiple
    candidates handles cases where the operator might mention the project
    either by its short name ("core") or its full name ("falcon-core").

    Double-counting across multiple patterns is avoided in
    :func:`compute_co_mention_graph` by taking the MAX hit count across all
    patterns for the same target project, rather than summing them.
    """
    slug = proj.cwd.lstrip("-")
    parts = [p for p in slug.split("-") if p]

    candidates: list[str] = []
    suffix_parts: list[str] = []
    for part in reversed(parts):
        suffix_parts.insert(0, part)
        name = "-".join(suffix_parts)
        if not _name_is_generic(name):
            candidates.append(name)
        if len(suffix_parts) >= 3:
            break

    # Return longest-first so that in compute_co_mention_graph we can take
    # the most specific match as the primary signal.
    return list(reversed(candidates))


def _collect_session_texts(
    projects_root: Path, projects: list[ProjectRecord]
) -> dict[str, list[str]]:
    """Return ``{cwd_slug: [text, ...]}`` — one text blob per session file.

    Each blob is the concatenation of all flattened record texts in a session.
    We collect every record (assistant + user) because cross-project mentions
    appear in both roles.
    """
    slug_set = {p.cwd for p in projects}
    result: dict[str, list[str]] = {p.cwd: [] for p in projects}

    for proj_dir in iter_project_dirs(projects_root):
        slug = proj_dir.name
        if slug not in slug_set:
            continue
        for session_file in _iter_session_files(proj_dir):
            lines: list[str] = []
            for rec in _iter_jsonl_records(session_file):
                text = _flatten_record_text(rec)
                if text:
                    lines.append(text)
            if lines:
                result[slug].append("\n".join(lines))

    return result


def compute_co_mention_graph(
    projects: list[ProjectRecord],
    session_texts: dict[str, list[str]],
    top_n: int = _CO_MENTION_TOP_N,
) -> dict[str, list[dict]]:
    """Compute a data-driven co-mention adjacency graph across projects.

    For each project P:
    - Scan the message text from all of P's sessions.
    - Count whole-word (``\\b``-bounded, case-insensitive) occurrences of
      every other project Q's candidate display names / basenames.
    - Rank by total mention count; keep top ``top_n``.

    Args:
        projects: All discovered project records.  Used to derive candidate
            names (from the slug only — no operator-specific literals).
        session_texts: ``{cwd_slug: [text_blob, ...]}`` — pre-collected
            session text for each project.  Pure inputs; this function does
            not perform any I/O.
        top_n: Maximum number of related projects to keep per project.

    Returns:
        ``{cwd: [{"cwd": str, "display_name": str, "mention_count": int}, ...]}``
        ordered by ``mention_count`` descending.  Projects with no
        cross-mentions get an empty list (never ``None``).

    Pure function — no side-effects, no I/O.
    """
    # Build a map from slug → list of compiled patterns for each candidate name.
    # Skip projects whose every candidate name is too generic.
    PatternEntry = tuple[str, re.Pattern]  # (candidate_name, pattern)
    project_patterns: dict[str, list[PatternEntry]] = {}
    for proj in projects:
        names = _candidate_names_for_project(proj)
        if not names:
            continue
        patterns: list[PatternEntry] = []
        for name in names:
            try:
                pat = re.compile(
                    r"\b" + re.escape(name) + r"\b",
                    re.IGNORECASE,
                )
                patterns.append((name, pat))
            except re.error:
                continue
        if patterns:
            project_patterns[proj.cwd] = patterns

    result: dict[str, list[dict]] = {}

    for proj in projects:
        my_cwd = proj.cwd
        texts = session_texts.get(my_cwd, [])
        if not texts:
            result[my_cwd] = []
            continue

        # Combined text for this project's sessions (cheap for regex scanning).
        combined = "\n".join(texts)
        counts: Counter[str] = Counter()

        for other_cwd, patterns in project_patterns.items():
            if other_cwd == my_cwd:
                continue  # skip self
            # Take the MAX across all candidate patterns for this project to
            # avoid double-counting when a shorter pattern matches inside a
            # longer one (e.g. "engine" inside "raptor-engine").
            max_hits = 0
            for _name, pat in patterns:
                hits = len(pat.findall(combined))
                if hits > max_hits:
                    max_hits = hits
            if max_hits:
                counts[other_cwd] = max_hits

        if not counts:
            result[my_cwd] = []
            continue

        # Build display-name lookup for the result records.
        slug_to_proj = {p.cwd: p for p in projects}

        ranked = counts.most_common(top_n)
        result[my_cwd] = [
            {
                "cwd": cwd,
                "display_name": slug_to_proj[cwd].display_name if cwd in slug_to_proj else cwd,
                "mention_count": cnt,
            }
            for cwd, cnt in ranked
        ]

    # Ensure every project has an entry (even if empty).
    for proj in projects:
        result.setdefault(proj.cwd, [])

    return result


# ---------------------------------------------------------------------------
# Record-stream entry point (multi-source collector path)
# ---------------------------------------------------------------------------


def _record_is_user_role(rec: Any) -> bool:
    """Return True if ``rec`` is a user-role turn, handling both dict and
    Record objects (lib.schema.UserRecord)."""
    if isinstance(rec, dict):
        msg = rec.get("message")
        if isinstance(msg, dict):
            return msg.get("role") == "user"
        return False
    # Record objects: UserRecord has a .text attribute and content_kind, but
    # role is encoded in the type field.  Check rec.type == "user" as the
    # cheapest proxy — that is exactly what the file-based path does.
    return str(getattr(rec, "type", "")) == "user"


def _extract_operator_tokens_from_rec(
    rec: Any,
    operator_usernames: frozenset[str] | None = None,
) -> list[str]:
    """Extract candidate vocabulary tokens from a user-role record.

    Handles both raw dict records (existing corpus path) and Record objects
    emitted by the multi-source collector.  Falls back to `.raw` for types
    that carry the original dict.
    """
    if isinstance(rec, dict):
        return _extract_operator_tokens(rec, operator_usernames)

    # Record object path: only user-role turns yield operator vocabulary.
    if not _record_is_user_role(rec):
        return []

    # Try .text first (UserRecord.text is the pre-extracted plain string).
    text: str | None = None
    t = getattr(rec, "text", None)
    if isinstance(t, str) and t:
        text = t
    else:
        # Fallback: try .raw (original dict) so _extract_operator_tokens works.
        raw = getattr(rec, "raw", None)
        if isinstance(raw, dict):
            return _extract_operator_tokens(raw, operator_usernames)

    if not text:
        return []

    tokens = _TOKEN_RE.findall(text)
    return [tok for tok in tokens if not _is_boring_token(tok, operator_usernames)]


def extract_ontology_from_records(records: Iterable[Any]) -> OntologySnapshot:
    """Mine MACHINES and VOCABULARY from a stream of session records.

    Accepts :class:`lib.schema.Record` objects from the multi-source
    collector OR raw JSONL-parsed dicts — any mix is handled.

    Returns an :class:`OntologySnapshot` with:
    - ``machines`` populated via the existing hostname + IP regex pipeline.
    - ``vocabulary`` populated via the same token-mining + specificity gate +
      English-wordlist filter used by :func:`mine_vocabulary`, with the same
      promotion thresholds (``VOCAB_MIN_SESSIONS`` / ``VOCAB_MIN_FREQ``).
    - ``projects`` **empty** — project rows come from the ``projects`` table
      already populated by the ingest path's directory walk.  Skip the
      project-dir walk here because we don't have the projects-root path and
      the co-mention graph (which needs full project slug lists) is a
      separate concern executed on the file-based :func:`extract_ontology`
      path during rebuild.

    The co-mention graph is **not** computed here — it requires a
    corpus-wide slug list + session-text collection and is not meaningful for
    a single incremental record batch.  It remains a rebuild-only, full-corpus
    concern in :func:`extract_ontology`.
    """
    snap = OntologySnapshot()
    machines: dict[str, MachineRecord] = {}

    # Vocab accumulators (mirror mine_vocabulary).
    term_sessions: dict[str, set[str]] = defaultdict(set)
    term_counts: Counter[str] = Counter()
    term_snippet: dict[str, str] = {}
    term_first_ts: dict[str, int] = {}

    # We can't derive operator usernames from a project-dir walk here;
    # skip the username-filter (minor — those names are caught by _STOPWORDS).
    operator_usernames: frozenset[str] = frozenset()

    for rec in records:
        # --- Normalise to a usable shape ---
        if isinstance(rec, dict):
            raw_dict: dict | None = rec
            rec_obj: Any = None
        else:
            _raw = getattr(rec, "raw", None)
            raw_dict = _raw if isinstance(_raw, dict) else None
            rec_obj = rec

        # --- Text extraction ---
        if raw_dict is not None:
            text = _flatten_record_text(raw_dict)
            ts_val = raw_dict.get("timestamp") or ""
            session_id = (
                raw_dict.get("sessionId") or raw_dict.get("session_id") or "_stream"
            )
        else:
            # Record object path.
            text = _record_to_text(rec_obj)
            ts_attr = getattr(rec_obj, "ts", None)
            if ts_attr is not None:
                try:
                    ts_val = ts_attr.isoformat()
                except AttributeError:
                    ts_val = str(ts_attr)
            else:
                ts_val = ""
            session_id = (
                getattr(rec_obj, "session_id", None) or "_stream"
            )

        # Parse timestamp to unix seconds for MachineRecord.last_seen_ts.
        ts_int = 0
        if ts_val:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                ts_int = int(dt.astimezone(timezone.utc).timestamp())
            except (ValueError, TypeError, AttributeError):
                pass

        # --- Machine extraction ---
        if text:
            _extract_machines_from_text(text, ts_int, machines)

        # --- Vocabulary extraction (user-role turns only) ---
        tokens = _extract_operator_tokens_from_rec(
            raw_dict if raw_dict is not None else rec_obj,
            operator_usernames,
        )
        if tokens:
            # Reconstruct text for snippet extraction when needed.
            rec_text = text
            for tok in tokens:
                key = tok.lower()
                term_sessions[key].add(session_id)
                term_counts[key] += 1
                if key not in term_snippet and rec_text:
                    snip = _snippet_for_term(tok, rec_text)
                    if snip:
                        term_snippet[key] = snip
                if key not in term_first_ts and ts_int:
                    term_first_ts[key] = ts_int

    snap.machines = sorted(machines.values(), key=lambda m: m.hostname)

    # Vocabulary: universal terms + promoted operator terms.
    now = int(time.time())
    seen_terms: set[str] = set()
    for v in _UNIVERSAL_CLAUDE_CODE_TERMS:
        snap.vocabulary.append(
            VocabularyTerm(
                term=v.term,
                definition=v.definition,
                category=v.category,
                frequency=1,
                first_seen_ts=now,
            )
        )
        seen_terms.add(v.term.lower())

    for key, sessions in term_sessions.items():
        if len(sessions) < VOCAB_MIN_SESSIONS:
            continue
        freq = term_counts[key]
        if freq < VOCAB_MIN_FREQ:
            continue
        if key in seen_terms:
            continue
        snap.vocabulary.append(
            VocabularyTerm(
                term=key,
                definition=term_snippet.get(key, ""),
                category="concept",
                frequency=freq,
                first_seen_ts=term_first_ts.get(key, 0),
            )
        )
        seen_terms.add(key)

    snap.vocabulary.sort(key=lambda v: v.frequency, reverse=True)
    return snap


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_ontology(conn, snap: OntologySnapshot) -> dict[str, int]:
    """Write a snapshot into the ontology tables.

    Returns counts ``{"projects": n, "machines": n, "vocabulary": n}``.
    Empty / zero-value fields are passed as ``None`` so COALESCE in the
    storage layer preserves anything that's already there.
    """
    from index.ontology import (
        upsert_machine,
        upsert_vocabulary_term,
    )

    written = {"projects": 0, "machines": 0, "vocabulary": 0}

    for p in snap.projects:
        # related_projects may be a list[dict] (co-mention graph output) or a
        # legacy list[str].  Serialize it directly so dict entries aren't
        # mangled by upsert_project's str() coercion.
        rp_json: str | None = None
        if p.related_projects:
            rp_json = json.dumps(p.related_projects, ensure_ascii=False)

        # Use a raw execute to inject the pre-serialized JSON, bypassing the
        # upsert_project helper's sorted-string-set logic for this field only.
        from index.ontology import ensure_schema as _ensure_schema
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO projects(
                cwd, display_name, purpose, primary_language,
                related_projects, first_seen_ts, last_active_ts, message_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cwd) DO UPDATE SET
                display_name     = COALESCE(excluded.display_name, projects.display_name),
                purpose          = COALESCE(excluded.purpose, projects.purpose),
                primary_language = COALESCE(excluded.primary_language, projects.primary_language),
                related_projects = COALESCE(excluded.related_projects, projects.related_projects),
                first_seen_ts    = COALESCE(
                    MIN(excluded.first_seen_ts, projects.first_seen_ts),
                    projects.first_seen_ts, excluded.first_seen_ts),
                last_active_ts   = COALESCE(
                    MAX(excluded.last_active_ts, projects.last_active_ts),
                    projects.last_active_ts, excluded.last_active_ts),
                message_count    = COALESCE(excluded.message_count, projects.message_count)
            """,
            (
                p.cwd,
                p.display_name or None,
                p.purpose or None,
                p.primary_language or None,
                rp_json,
                p.first_seen_ts or None,
                p.last_active_ts or None,
                p.message_count or None,
            ),
        )
        written["projects"] += 1

    for m in snap.machines:
        upsert_machine(
            conn,
            hostname=m.hostname,
            role=m.role or None,
            lan_ip=m.lan_ip or None,
            tailscale_ip=m.tailscale_ip or None,
            public_ip=m.public_ip or None,
            gpu=m.gpu or None,
            os=m.os or None,
            notes=m.notes or None,
            last_seen_ts=m.last_seen_ts or None,
        )
        written["machines"] += 1

    for v in snap.vocabulary:
        upsert_vocabulary_term(
            conn,
            term=v.term,
            definition=v.definition,
            category=v.category,
            frequency=v.frequency,
            first_seen_ts=v.first_seen_ts or None,
        )
        written["vocabulary"] += 1

    return written


# ---------------------------------------------------------------------------
# Incremental vocabulary count bump (Stop-hook hot path)
# ---------------------------------------------------------------------------


def _record_to_text(rec: Any) -> str:
    """Best-effort text extraction from a Record-like object or raw dict."""
    if isinstance(rec, dict):
        return _flatten_record_text(rec)

    # Record-like: try .text first, then assistant content blocks, then raw.
    t = getattr(rec, "text", None)
    if isinstance(t, str) and t:
        return t
    parts: list[str] = []
    blocks = getattr(rec, "content", None) or []
    for b in blocks:
        btype = getattr(b, "type", None)
        bt = getattr(b, "text", None)
        if btype == "text" and isinstance(bt, str):
            parts.append(bt)
    if parts:
        return "\n".join(parts)
    raw = getattr(rec, "raw", None)
    if isinstance(raw, dict):
        return _flatten_record_text(raw)
    return ""


def update_vocabulary_counts(records: Iterable[Any], conn) -> int:
    """Bump ``vocabulary.frequency`` for terms observed in ``records``.

    Incremental counter path used by the ingest Stop hook. Counts tokens
    from operator (user-role) turns only and bumps any term already in the
    ``vocabulary`` table. New term promotion (cross-session corroboration)
    is handled by ``mine_vocabulary`` during the full-corpus pass, not here.

    Returns the count of distinct vocabulary terms that received at least
    one increment.
    """
    from index.ontology import ensure_schema

    ensure_schema(conn)

    # Fetch current vocabulary terms so we can match against them.
    rows = conn.execute("SELECT term FROM vocabulary").fetchall()
    known_terms: set[str] = {row[0].lower() for row in rows}
    if not known_terms:
        return 0

    # Build match patterns for known terms only (no seed needed).
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for term in known_terms:
        escaped = r"\s+".join(re.escape(p) for p in term.split())
        patterns.append((term, re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)))

    counts: Counter[str] = Counter()
    int(time.time())
    for rec in records:
        text = _record_to_text(rec)
        if not text:
            continue
        # Only count user-role turns to stay consistent with mine_vocabulary.
        if isinstance(rec, dict):
            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("role") != "user":
                continue
        for term, pat in patterns:
            hits = len(pat.findall(text))
            if hits:
                counts[term] += hits

    touched = 0
    for term, hits in counts.items():
        if hits <= 0:
            continue
        conn.execute(
            "UPDATE vocabulary SET frequency = frequency + ? WHERE LOWER(term) = ?",
            (hits, term),
        )
        touched += 1
    return touched
