"""Operator-profile extractor.

Mines the ``~/.claude/projects/*.jsonl`` corpus for *who the operator is*:
their name, handles, machines they own, vendors they prefer / ban, and
recurring philosophy ("KISS", "single-dev maintainer", etc.).

Detection is fully data-driven — no operator-specific seeds are baked in.
Every identity signal (handles, orgs, timezones, products, billing rails,
hostnames) is derived from patterns in the corpus and ranked by frequency.
The most-frequent candidate wins each field.

Output is a single :class:`OperatorProfile` dataclass (not a stream of
:class:`extractors.base.Extraction` rows) — there's exactly one operator
per machine, so a streaming-candidates API would just add noise. The
companion :mod:`index.operator` module persists the result as key/value
rows in the ``operator_profile`` SQLite table so the MCP layer can serve
the profile to future sessions cheaply.

Heuristic detail:

* **Emails**: collected by frequency; the most-common is ``email_primary``,
  the rest land in ``emails_alt``.
* **Names**: literal ``git config user.name "X"`` lines
  + ``author = "X"`` in pyproject-style snippets.
* **Handles**: derived from ``github.com/<user>``, ``gitlab.com/<user>``,
  git remote URLs, and ``@mention`` patterns; ranked by frequency.
* **Org**: derived from email domains, ``author``/``org`` fields, and
  company-name mentions near the operator's name; ranked by frequency.
* **Machines**: hostnames found in ssh/deploy/hostname contexts (tokens
  appearing as ``ssh user@<host>``, ``deploy to <host>``, etc.) plus
  Tailscale CGNAT (``100.64.0.0/10``) IPs and LAN (RFC1918) IPs.
* **Timezone**: any IANA zone (``Region/City``) or common abbreviation;
  mapped generically to canonical IANA zones, ranked by frequency.
* **Billing rail**: any major processor (paypal, stripe, paddle, etc.);
  ranked by frequency.
* **Own products**: derived from git remote repo names and ``our/my <X>
  project`` references; ranked by frequency.
* **Banned providers**: phrases like ``never recommend <provider>``.
* **Philosophy**: standing-rule statements (KISS, "single dev", etc.) with
  confidence proportional to occurrence frequency, capped at 1.0.

Designed to be cheap (single pass, regex-only) so it can run inside the
regular extraction pipeline without slowing it materially.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "OperatorProfile",
    "extract_operator_profile",
    "extract_operator_profile_from_records",
    "extract_incremental",
    "persist_profile",
    "persist_incremental_profile",
]


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class OperatorProfile:
    """Snapshot of the operator's identity, as mined from session logs."""

    name: str = ""
    handle: str = ""
    org: str = ""
    email_primary: str = ""
    emails_alt: list[str] = field(default_factory=list)
    github_user: str = ""
    gitlab_host: str = ""
    timezone: str = ""
    machines: dict[str, dict] = field(default_factory=dict)
    home_uplinks: list[str] = field(default_factory=list)
    default_cloud_provider: str = ""
    banned_providers: list[str] = field(default_factory=list)
    billing_rail: str = ""
    scm_policy: str = ""
    philosophy: list[str] = field(default_factory=list)
    own_products: list[str] = field(default_factory=list)
    confidence: dict[str, float] = field(default_factory=dict)
    sources: dict[str, list[str]] = field(default_factory=dict)

    def to_field_dict(self) -> dict[str, Any]:
        """Return ``{field_name: value}`` for the storable scalar fields.

        Excludes ``confidence`` and ``sources`` — those are auxiliary metadata
        that :func:`persist_profile` writes per-field, not as their own row.
        """
        return {
            "name": self.name,
            "handle": self.handle,
            "org": self.org,
            "email_primary": self.email_primary,
            "emails_alt": self.emails_alt,
            "github_user": self.github_user,
            "gitlab_host": self.gitlab_host,
            "timezone": self.timezone,
            "machines": self.machines,
            "home_uplinks": self.home_uplinks,
            "default_cloud_provider": self.default_cloud_provider,
            "banned_providers": self.banned_providers,
            "billing_rail": self.billing_rail,
            "scm_policy": self.scm_policy,
            "philosophy": self.philosophy,
            "own_products": self.own_products,
        }


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_FULL_NAME_RE = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")
_GIT_USER_NAME_RE = re.compile(
    r"""git\s+config\s+user\.name\s+["']([^"']+)["']""", re.IGNORECASE
)
_AUTHOR_RE = re.compile(
    r"""author\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
# Hostname: tokens appearing in ssh/deploy/hostname invocation contexts.
# Captures identifiers that look like machine names (contain a hyphen or
# start with a known prefix like relay-, host-, node-) in positional
# contexts that indicate the token IS the target host.
_HOSTNAME_CONTEXT_RE = re.compile(
    r"""(?:ssh\s+(?:[A-Za-z0-9_]+@)?([\w][\w.-]{1,48}[\w])"""
    r"""|deploy(?:ing|ed)?\s+to\s+([\w][\w.-]{1,48}[\w])"""
    r"""|hostname[:\s]+([\w][\w.-]{1,48}[\w]))""",
    re.IGNORECASE,
)
# Also match repeated hyphenated identifiers that look like hostnames
# (e.g. relay-sin-01, web-prod-3) — at least one hyphen, no dots.
_HOSTNAME_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+){1,4})\b")

def _looks_like_person_name(s: str) -> bool:
    """Reject free-text two-cap matches that are obviously product/service
    names, not people. ``Claude Code`` / ``Sidecar Network`` / ``Cloudflare
    Tunnel`` all get rejected because ``Code/Network/Tunnel`` are common
    product nouns. ``Dana Lopez`` / ``Sam Rivera`` survive.
    """
    parts = (s or "").split()
    if len(parts) != 2:
        return False
    if parts[1] in _PRODUCT_NOUN_SUFFIXES:
        return False
    return True


_PRODUCT_NOUN_SUFFIXES: frozenset[str] = frozenset({
    "Code", "Tool", "Tools", "Service", "Services", "Cloud", "Network",
    "Networks", "Action", "Actions", "Request", "Requests", "Tunnel",
    "Worker", "Workers", "Engine", "Engines", "Studio", "Platform",
    "Functions", "Pages", "Sites", "Bot", "Bots", "API", "APIs", "SDK",
    "CLI", "App", "Apps", "Hub", "Router", "Routers", "Server", "Servers",
    "Mono", "Sans", "Serif", "Card", "Cards", "Notification", "Notifications",
    "Token", "Tokens", "Opus", "Sonnet", "Haiku", "Pro", "Plus", "Max",
    "Mini", "Lite", "Connector", "Connectors", "Plugin", "Plugins",
    "Module", "Modules", "Turnstile", "Manager", "Console", "Dashboard",
    "Micro", "Macro", "Edge", "Core", "Stack", "Suite", "Studio", "Lab",
    "Labs", "Gateway", "Proxy", "Filter", "Queue", "Stream", "Event",
})


def _looks_like_real_name(s: str) -> bool:
    """Reject placeholders + obvious non-names from git/author captures.

    Transcripts frequently quote *instructions* like ``git config user.name "X"``
    or ``author = "your name"`` where ``X`` / ``your name`` is a literal
    placeholder, not a real identity. Filter these so they don't outrank
    actual operator names.
    """
    s = (s or "").strip()
    if len(s) < 3 or len(s) > 80:
        return False
    sl = s.lower()
    if sl in _NAME_PLACEHOLDERS:
        return False
    # Must contain at least one alphabetic char.
    if not any(c.isalpha() for c in s):
        return False
    return True


_NAME_PLACEHOLDERS: frozenset[str] = frozenset({
    "x", "y", "z", "foo", "bar", "baz", "qux", "name", "your name",
    "yourname", "user", "username", "operator", "the operator",
    "first last", "firstname lastname", "first.last", "test", "test user",
    "example", "sample", "default", "todo", "tbd", "n/a", "none", "null",
    "claude", "claude code", "anthropic", "ai assistant",
})


# Common English words that may follow ``ssh``/``deploy``/``hostname`` in
# prose and would otherwise be mis-matched as machine names. Not exhaustive
# — just the high-frequency false-positives observed in real corpora.
_HOSTNAME_COMMON_WORDS: frozenset[str] = frozenset({
    "the", "this", "that", "your", "our", "their", "his", "her", "its",
    "for", "from", "into", "onto", "with", "without", "after", "before",
    "key", "keys", "file", "files", "path", "paths", "session", "sessions",
    "shell", "user", "users", "remote", "local", "host", "server", "client",
    "agent", "service", "config", "log", "logs", "data", "code", "script",
    "command", "process", "thread", "port", "address", "domain", "name",
    "void", "null", "any", "all", "some", "none", "off", "on", "up", "down",
    "pubkey", "creds", "secret", "secrets", "password", "passwd", "token",
    "what", "which", "where", "when", "why", "how", "who",
    "exposures", "exposure", "again", "now", "later", "today", "tomorrow",
})
_TAILSCALE_RE = re.compile(r"\b(100\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_LAN_RE = re.compile(
    r"\b((?:10\.\d{1,3}\.\d{1,3}\.\d{1,3})"
    r"|(?:172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
    r"|(?:192\.168\.\d{1,3}\.\d{1,3}))\b"
)
_BANNED_RE = re.compile(
    r"(?i)never\s+(?:ever\s+)?(?:recommend|use|mention)\s+(?:or\s+\w+\s+)?(\w+)"
)

# Handle: derive from github.com/<user>, gitlab.com/<user>, git remote URLs,
# and @mention patterns.
_HANDLE_GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})")
_HANDLE_GITLAB_RE = re.compile(r"gitlab\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})")
_HANDLE_GIT_REMOTE_RE = re.compile(
    r"(?:git@[\w.-]+:|https?://[\w.-]+/)([A-Za-z0-9][A-Za-z0-9_-]{0,38})/[\w.-]+\.git"
)
_HANDLE_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_-]{1,38})\b")

# GitHub user (separate from handle — used for github_user field specifically)
_GITHUB_USER_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})")
_GITLAB_HOST_RE = re.compile(
    r"(?:gitlab[\s@]|self.?hosted\s+gitlab.*?)\b([a-z0-9][a-z0-9\-]+\.[a-z]{2,})\b",
    re.IGNORECASE,
)

# Org: derive from email domain, author/org fields, and company mentions near
# the operator name.
_ORG_FROM_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)\.[A-Za-z]{2,}"
)
_ORG_FIELD_RE = re.compile(
    r"""(?:org(?:anization)?|company)\s*[:=]\s*["']?([A-Za-z0-9][A-Za-z0-9 &,._-]{1,60})""",
    re.IGNORECASE,
)

# Timezone: anchored IANA zone — must start with a known geographic region
# prefix so that code paths like ``Jc/Jmin/Jmax`` and prose like
# ``Pull/Request`` are never mistaken for timezone values.
_IANA_REGION_PREFIX = (
    r"(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia"
    r"|Europe|Indian|Pacific|US|Etc|GMT)"
)
_TIMEZONE_IANA_RE = re.compile(
    r"\b(" + _IANA_REGION_PREFIX + r"/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b"
)
# Fast set of valid IANA region prefixes for the post-extraction filter.
_IANA_VALID_REGIONS: frozenset[str] = frozenset({
    "Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic",
    "Australia", "Europe", "Indian", "Pacific", "US", "Etc", "GMT",
})
# Common abbreviation → IANA mapping (generic, not Eastern-biased).
_TZ_ABBREV: dict[str, str] = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "GMT": "Europe/London",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "EET": "Europe/Helsinki",
    "EEST": "Europe/Helsinki",
    "IST": "Asia/Kolkata",
    "JST": "Asia/Tokyo",
    "KST": "Asia/Seoul",
    "CST_CHINA": "Asia/Shanghai",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
    "NZST": "Pacific/Auckland",
    "NZDT": "Pacific/Auckland",
    "UTC": "UTC",
}
_TZ_ABBREV_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _TZ_ABBREV) + r")\b"
)
# Also match US/... aliases.
_TZ_US_ALIAS_RE = re.compile(r"\b(US/(?:Eastern|Central|Mountain|Pacific|Alaska|Hawaii))\b")
_US_ALIAS_MAP: dict[str, str] = {
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "US/Alaska": "America/Anchorage",
    "US/Hawaii": "Pacific/Honolulu",
}

# Billing rail: generic reference set of major processors.
_BILLING_RAIL_RE = re.compile(
    r"\b(paypal|stripe|paddle|lemonsqueezy|lemon\s+squeezy|braintree|chargebee|"
    r"recurly|fastspring|gumroad|woocommerce|shopify\s+payments|square)\b",
    re.IGNORECASE,
)

# ISP / uplink: generic reference list — no single-operator bias.
_UPLINK_RE = re.compile(
    r"\b(Starlink|SpaceX\s+Starlink|Frontier|Verizon|Comcast|Xfinity|Spectrum|"
    r"Cox|AT&T|T-Mobile\s+Home|CenturyLink|Lumen|Ziply|Wave|Optimum|Altice|"
    r"WOW|Windstream|Kinetic|Breezeline)\b",
    re.IGNORECASE,
)

# Own products: derive from "our <X> project", "my <X> project", git remote
# repo names (last path component before .git).
_OWN_PRODUCT_PHRASE_RE = re.compile(
    r"""(?:our|my)\s+([\w][\w-]{1,40})\s+(?:project|tool|plugin|service|app|library|lib|repo)\b""",
    re.IGNORECASE,
)
_OWN_PRODUCT_GIT_REPO_RE = re.compile(
    r"""(?:git@[\w.-]+:|https?://[\w.-]+/[\w.-]+/)([A-Za-z0-9][\w.-]{0,60})\.git""",
    re.IGNORECASE,
)

# Philosophy phrases — match common standing-rule statements.
_PHILOSOPHY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bKISS\b"), "KISS"),
    (re.compile(r"\bsingle[-\s]?dev\b", re.IGNORECASE), "single-dev maintainer"),
    (re.compile(r"\bself[-\s]?host(?:ed|ing)?\b", re.IGNORECASE), "self-hosted"),
    (re.compile(r"\bverify\s+before\s+assert", re.IGNORECASE), "verify before asserting"),
    (re.compile(r"\bbias\s+to\s+action\b", re.IGNORECASE), "bias to action"),
    (re.compile(r"\bfour\s+ds\b", re.IGNORECASE), "Four Ds filter"),
    (re.compile(r"\bMCP[-\s]?first\b", re.IGNORECASE), "MCP-first"),
]


# Defaults from the research brief — used when the corpus is silent but the
# field has a well-known canonical value. Confidence is correspondingly low.
_DEFAULT_HINTS: dict[str, tuple[Any, float]] = {}



# ---------------------------------------------------------------------------
# Iteration helpers
# ---------------------------------------------------------------------------


def _iter_jsonl_records(path: Path) -> Iterable[tuple[int, dict]]:
    """Yield ``(line_no, parsed_record)`` from a JSONL file.

    Malformed lines are skipped silently — extractors are best-effort
    and the corpus occasionally contains partial writes.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield idx, json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:  # pragma: no cover — filesystem race
        log.debug("could not read %s: %s", path, exc)
        return


def _extract_text_from_record(rec: dict) -> str:
    """Flatten the text content of a single JSONL record to a single string.

    Handles both legacy "content as string" and the modern
    ``message.content = [{type:text, text:...}, ...]`` shape.
    """
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
    # Some record types put text at the top level.
    for key in ("content", "text", "title"):
        v = rec.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------


def _iter_text_from_paths(
    jsonl_paths: Iterable[Path],
) -> Iterable[tuple[str, str, int]]:
    """Yield ``(text, source, line_no)`` triples by walking JSONL files."""
    for path in jsonl_paths:
        path = Path(path)
        for line_no, rec in _iter_jsonl_records(path):
            text = _extract_text_from_record(rec)
            if text:
                yield text, str(path), line_no


def _iter_text_from_records(
    records: Iterable[Any],
) -> Iterable[tuple[str, str, int]]:
    """Yield ``(text, source, line_no)`` triples from in-memory records.

    Accepts both :class:`lib.schema.Record` instances (using
    ``source_file`` + ``byte_offset`` for citation) and raw dicts of the
    Claude Code JSONL shape (``message.content`` either string or list).
    """
    for rec in records:
        text: str = ""
        source: str = "<record>"
        line_no: int = 0
        if isinstance(rec, dict):
            text = _extract_text_from_record(rec)
            source = str(rec.get("source_file", "<record>"))
            line_no = int(rec.get("byte_offset", 0) or 0)
        else:
            # Record-like: prefer .text, then assistant content blocks.
            t = getattr(rec, "text", None)
            if isinstance(t, str) and t:
                text = t
            else:
                blocks = getattr(rec, "content", None) or []
                parts: list[str] = []
                for b in blocks:
                    btype = getattr(b, "type", None)
                    bt = getattr(b, "text", None)
                    if btype == "text" and isinstance(bt, str):
                        parts.append(bt)
                if parts:
                    text = "\n".join(parts)
            source_attr = getattr(rec, "source_file", None)
            source = str(source_attr) if source_attr else "<record>"
            line_no = int(getattr(rec, "byte_offset", 0) or 0)
        if text:
            yield text, source, line_no


def _extract_from_text_stream(
    stream: Iterable[tuple[str, str, int]],
) -> OperatorProfile:
    """Shared mining body: consume ``(text, source, line_no)`` triples."""
    profile = OperatorProfile()

    email_counts: Counter[str] = Counter()
    name_counts: Counter[str] = Counter()
    explicit_name_counts: Counter[str] = Counter()
    machine_hits: dict[str, dict[str, Any]] = {}
    tailscale_ips: list[str] = []
    lan_ips: list[str] = []
    banned: Counter[str] = Counter()
    handles: Counter[str] = Counter()
    orgs: Counter[str] = Counter()
    github_users: Counter[str] = Counter()
    gitlab_hosts: Counter[str] = Counter()
    timezones: Counter[str] = Counter()
    billing: Counter[str] = Counter()
    uplinks: Counter[str] = Counter()
    own_prods: Counter[str] = Counter()
    philosophy_hits: Counter[str] = Counter()

    sources: dict[str, list[str]] = {}

    def _cite(key: str, source: str, line_no: int, max_cites: int = 5) -> None:
        bucket = sources.setdefault(key, [])
        cite = f"{source}:{line_no}"
        if cite not in bucket and len(bucket) < max_cites:
            bucket.append(cite)

    for text, source, line_no in stream:
        if not text:
            continue

        for m in _EMAIL_RE.findall(text):
            email_counts[m.lower()] += 1
            _cite("email", source, line_no)

        # Name signals split by source authority. Explicit identity
        # declarations (`git config user.name "X"`, `author = "X"`) are
        # CATEGORICALLY stronger than free-text two-cap matches: any number
        # of "Claude Code" mentions in transcripts cannot outrank a single
        # explicit `git config user.name "Sam Rivera"` declaration.
        # Free-text is only used when no explicit source exists.
        for m in _FULL_NAME_RE.findall(text):
            if _looks_like_person_name(m):
                name_counts[m] += 1
                _cite("name", source, line_no)
        for m in _GIT_USER_NAME_RE.findall(text):
            if _looks_like_real_name(m):
                explicit_name_counts[m] += 1
                _cite("name", source, line_no)
        for m in _AUTHOR_RE.findall(text):
            if _looks_like_real_name(m):
                explicit_name_counts[m] += 1
                _cite("name", source, line_no)

        # Hostnames: context-anchored (ssh/deploy/hostname lines). The
        # context regex matches the first word after `ssh`/`deploy to`/etc,
        # which on free-text prose picks up common English words ("ssh key",
        # "deploy to production for your remote file"). Filter the candidate
        # against a short common-word stoplist and require either a hyphen,
        # a digit, a dot, OR a non-common token of length>=5.
        for m in _HOSTNAME_CONTEXT_RE.finditer(text):
            # Groups 1-3 correspond to the three alternatives.
            host = m.group(1) or m.group(2) or m.group(3) or ""
            host = host.lower().strip(".")
            if not (host and len(host) >= 3):
                continue
            # Reject pure IPs — those land in lan_ips / tailscale_ips.
            if all(c.isdigit() or c == "." for c in host):
                continue
            # Real hostnames almost always have a hyphen, digit, or dot
            # (e.g. relay-eu-1, host01, gw.example). Bare alphabetic
            # tokens are accepted only when long AND not common English.
            looks_like_host = (
                "-" in host or "." in host or any(c.isdigit() for c in host)
                or (len(host) >= 8 and host not in _HOSTNAME_COMMON_WORDS)
            )
            if not looks_like_host:
                continue
            if host in _HOSTNAME_COMMON_WORDS:
                continue
            # Strip a trailing dot (the regex captures one) — already done above.
            slot = machine_hits.setdefault(
                host, {"role": "", "ip": "", "tailscale": "", "hits": 0}
            )
            slot["hits"] += 1
            _cite("machines", source, line_no)

        for ip in _TAILSCALE_RE.findall(text):
            tailscale_ips.append(ip)
            _cite("machines", source, line_no)
        for ip in _LAN_RE.findall(text):
            lan_ips.append(ip)
            _cite("machines", source, line_no)

        for victim in _BANNED_RE.findall(text):
            v = victim.lower().strip()
            if v and v not in {"to", "the", "any", "all", "it"}:
                banned[v] += 1
                _cite("banned_providers", source, line_no)

        # Handle: aggregate from github.com/<user>, gitlab.com/<user>,
        # git remote URLs, and @mentions; rank by frequency.
        _HANDLE_STOPLIST = {
            "anthropic", "anthropics", "claude", "claude-code", "openai",
            "google", "microsoft", "facebook", "meta", "aws", "amazon",
        }
        for h in _HANDLE_GITHUB_RE.findall(text):
            hk = h.lower()
            if hk not in _HANDLE_STOPLIST:
                handles[hk] += 1
                _cite("handle", source, line_no)
        for h in _HANDLE_GITLAB_RE.findall(text):
            hk = h.lower()
            if hk not in _HANDLE_STOPLIST:
                handles[hk] += 1
                _cite("handle", source, line_no)
        for h in _HANDLE_GIT_REMOTE_RE.findall(text):
            hk = h.lower()
            if hk not in _HANDLE_STOPLIST:
                handles[hk] += 1
                _cite("handle", source, line_no)
        for h in _HANDLE_MENTION_RE.findall(text):
            hk = h.lower()
            if hk not in _HANDLE_STOPLIST and len(hk) >= 3:
                handles[hk] += 1
                _cite("handle", source, line_no)

        # Org: derive from email domain tokens, org/company fields.
        _ORG_DOMAIN_STOPLIST = {
            "gmail", "yahoo", "hotmail", "outlook", "icloud", "proton",
            "protonmail", "fastmail", "hey", "duck", "anthropic", "claude",
            "github", "gitlab", "google", "microsoft", "apple", "amazon",
        }
        for domain in _ORG_FROM_EMAIL_RE.findall(text):
            # Use the second-level domain token as the org hint.
            parts = domain.lower().rstrip(".").split(".")
            token = parts[-2] if len(parts) >= 2 else parts[0]
            if token and token not in _ORG_DOMAIN_STOPLIST and len(token) >= 3:
                orgs[token] += 1
                _cite("org", source, line_no)
        for org_name in _ORG_FIELD_RE.findall(text):
            ot = org_name.strip()
            if ot:
                orgs[ot] += 1
                _cite("org", source, line_no)

        for gh in _GITHUB_USER_RE.findall(text):
            ghn = gh.lower()
            # Filter obvious non-personal users (orgs / project names get
            # captured here too; we just rank by frequency below).
            github_users[ghn] += 1
            _cite("github_user", source, line_no)

        for h in _GITLAB_HOST_RE.findall(text):
            gitlab_hosts[h] += 1
            _cite("gitlab_host", source, line_no)

        # Timezone: IANA zones + abbreviations, all mapped generically.
        # The regex already anchors to known region prefixes; no extra
        # check needed here — just count what matched.
        for tz in _TIMEZONE_IANA_RE.findall(text):
            timezones[tz] += 1
            _cite("timezone", source, line_no)
        for abbr in _TZ_ABBREV_RE.findall(text):
            iana = _TZ_ABBREV.get(abbr, abbr)
            timezones[iana] += 1
            _cite("timezone", source, line_no)
        for alias in _TZ_US_ALIAS_RE.findall(text):
            iana = _US_ALIAS_MAP.get(alias, alias)
            timezones[iana] += 1
            _cite("timezone", source, line_no)

        for b in _BILLING_RAIL_RE.findall(text):
            # Normalize multi-word names.
            bk = re.sub(r"\s+", "", b.lower())
            billing[bk] += 1
            _cite("billing_rail", source, line_no)

        for u in _UPLINK_RE.findall(text):
            # Normalise whitespace for multi-word names.
            uk = re.sub(r"\s+", " ", u.strip())
            uplinks[uk] += 1
            _cite("home_uplinks", source, line_no)

        # Own products: "our/my <name> project/tool/..." and git repo names.
        for prod in _OWN_PRODUCT_PHRASE_RE.findall(text):
            pk = prod.lower().strip()
            if pk:
                own_prods[pk] += 1
                _cite("own_products", source, line_no)
        for repo in _OWN_PRODUCT_GIT_REPO_RE.findall(text):
            rk = repo.lower().strip(".-")
            if rk and len(rk) >= 3:
                own_prods[rk] += 1
                _cite("own_products", source, line_no)

        for pat, label in _PHILOSOPHY_PATTERNS:
            if pat.search(text):
                philosophy_hits[label] += 1
                _cite("philosophy", source, line_no)

    # ----- Reduce to single-best values + confidence ------------------------

    if email_counts:
        primary_email, primary_count = email_counts.most_common(1)[0]
        profile.email_primary = primary_email
        profile.emails_alt = [e for e, _ in email_counts.most_common()[1:6]]
        profile.confidence["email_primary"] = min(1.0, 0.5 + 0.1 * primary_count)
    if explicit_name_counts:
        # Authoritative source wins outright.
        best_name, n = explicit_name_counts.most_common(1)[0]
        profile.name = best_name
        profile.confidence["name"] = min(1.0, 0.7 + 0.1 * n)
    elif name_counts:
        # Filtered free-text wins over email-derived: real "First Last"
        # mentions are better signal than capitalizing an email local part.
        best_name, n = name_counts.most_common(1)[0]
        profile.name = best_name
        profile.confidence["name"] = min(0.7, 0.4 + 0.05 * n)
    elif profile.email_primary and "@" in profile.email_primary:
        # Last-resort: derive a candidate from the email local part.
        # ``dana@example.com`` → ``Dana``. Honest when the corpus has
        # no explicit git/author declaration and no person-name mentions.
        local = profile.email_primary.split("@", 1)[0]
        local_main = re.split(r"[._+\-]", local)[0]
        if local_main and local_main.isalpha() and len(local_main) >= 2:
            profile.name = local_main.capitalize()
            profile.confidence["name"] = 0.45
    if handles:
        # Reinforce candidates that corroborate the operator's own identity:
        # a handle matching the email local-part, the email SLD (second-level
        # domain label), or a name token gets a strong frequency boost so it
        # beats unrelated high-frequency tokens (e.g. a frequently-mentioned
        # project like ``opnsense`` can't outrank ``dana`` when the email is
        # ``dana@example.com``).
        identity_tokens: set[str] = set()
        if profile.email_primary and "@" in profile.email_primary:
            local, domain = profile.email_primary.split("@", 1)
            # Local part and each dot/dash component (e.g. "dana.m" → "dana").
            identity_tokens.add(local.lower())
            for tok in re.split(r"[._+\-]", local):
                if tok:
                    identity_tokens.add(tok.lower())
            # Second-level domain label (e.g. "88plug.com" → "88plug").
            domain_parts = domain.rstrip(".").split(".")
            if len(domain_parts) >= 2:
                identity_tokens.add(domain_parts[-2].lower())
        if profile.name:
            for tok in profile.name.lower().split():
                if len(tok) >= 2:
                    identity_tokens.add(tok)
        # Apply a 10x frequency boost for identity-corroborating candidates.
        IDENTITY_BOOST = 10
        boosted: Counter[str] = Counter()
        for cand, cnt in handles.items():
            boosted[cand] = cnt * IDENTITY_BOOST if cand in identity_tokens else cnt
        best_handle, raw_count = boosted.most_common(1)[0]
        profile.handle = best_handle
        profile.confidence["handle"] = min(1.0, 0.5 + 0.1 * handles[best_handle])
    if orgs:
        profile.org, n = orgs.most_common(1)[0]
        profile.confidence["org"] = min(1.0, 0.5 + 0.1 * n)
    if github_users:
        # Drop the literal org/repo we recognise as not personal.
        for gh, _n in github_users.most_common():
            if gh not in {"anthropics", "anthropic", "claude-code"}:
                profile.github_user = gh
                profile.confidence["github_user"] = min(1.0, 0.5 + 0.05 * _n)
                break
    if gitlab_hosts:
        profile.gitlab_host, n = gitlab_hosts.most_common(1)[0]
        profile.confidence["gitlab_host"] = min(1.0, 0.5 + 0.1 * n)
    if timezones:
        # Only accept a timezone that matches the anchored IANA pattern or is
        # a valid abbreviation-mapped zone. The counter may have accumulated
        # values from abbreviation mapping (e.g. "UTC", "America/New_York")
        # that are safe; anything else that slipped through is rejected so we
        # leave timezone empty rather than returning garbage.
        _tz_iana_check = re.compile(
            r"^(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia"
            r"|Europe|Indian|Pacific|US|Etc|GMT)/[A-Za-z_]+(?:/[A-Za-z_]+)?$"
        )
        _valid_abbrev_zones = set(_TZ_ABBREV.values())
        for tz_cand, n in timezones.most_common():
            if tz_cand in _valid_abbrev_zones or _tz_iana_check.match(tz_cand):
                profile.timezone = tz_cand
                profile.confidence["timezone"] = min(1.0, 0.5 + 0.1 * n)
                break
        # If nothing valid found, timezone stays empty (better than garbage).
    if billing:
        profile.billing_rail, n = billing.most_common(1)[0]
        profile.confidence["billing_rail"] = min(1.0, 0.5 + 0.1 * n)
    if uplinks:
        profile.home_uplinks = [u for u, _ in uplinks.most_common()]
        profile.confidence["home_uplinks"] = min(1.0, 0.4 + 0.1 * sum(uplinks.values()))
    if banned:
        profile.banned_providers = [b for b, _ in banned.most_common()]
        profile.confidence["banned_providers"] = min(
            1.0, 0.5 + 0.1 * sum(banned.values())
        )
    if own_prods:
        profile.own_products = [p for p, _ in own_prods.most_common()]
        profile.confidence["own_products"] = min(
            1.0, 0.4 + 0.05 * sum(own_prods.values())
        )
    if philosophy_hits:
        profile.philosophy = [p for p, _ in philosophy_hits.most_common()]
        # Confidence per slogan is proportional to its hit count, max 1.0.
        profile.confidence["philosophy"] = min(
            1.0, 0.4 + 0.05 * sum(philosophy_hits.values())
        )

    # Machines: best-effort enrichment with the collected IPs. We don't try
    # to perfectly map each IP to a host — sessions usually mention them
    # adjacently, but doing that reliably requires sentence-level windowing
    # we deliberately avoid in this pass. We surface what we have and let
    # downstream callers refine.
    if machine_hits:
        for host, slot in machine_hits.items():
            profile.machines[host] = {
                "role": slot.get("role", ""),
                "ip": "",
                "tailscale": "",
                "hits": slot["hits"],
            }
        # Attach the first tailscale / LAN IP globally so the dict isn't empty.
        if tailscale_ips:
            first_host = next(iter(profile.machines))
            profile.machines[first_host]["tailscale"] = tailscale_ips[0]
        if lan_ips:
            first_host = next(iter(profile.machines))
            profile.machines[first_host]["ip"] = lan_ips[0]
        profile.confidence["machines"] = min(
            1.0, 0.4 + 0.05 * sum(s["hits"] for s in machine_hits.values())
        )

    # default_cloud_provider is not inferred from static defaults — it is
    # derived from standing_decisions at query time so it reflects whoever
    # the operator actually is (not a compile-time assumption).

    # Fold in static hints (low confidence).
    for key, (val, conf) in _DEFAULT_HINTS.items():
        if not getattr(profile, key, ""):
            setattr(profile, key, val)
            profile.confidence.setdefault(key, conf)

    profile.sources = sources
    return profile


def extract_operator_profile(jsonl_paths: Iterable[Path]) -> OperatorProfile:
    """Mine every passed JSONL file for operator identity signals.

    Returns a fully populated :class:`OperatorProfile` (fields that had no
    signal are left at their dataclass defaults). Per-field confidence is
    set based on how strong the evidence was; ``sources`` accumulates
    ``"path:line"`` citations capped at a small number per field to keep
    the persisted JSON sane.
    """
    return _extract_from_text_stream(_iter_text_from_paths(jsonl_paths))


def extract_operator_profile_from_records(records: Iterable[Any]) -> OperatorProfile:
    """Mine an in-memory record stream for operator identity signals.

    Accepts both :class:`lib.schema.Record` instances (as yielded by
    :func:`lib.sources.collect.iter_all_source_records`) and raw Claude Code
    JSONL dicts.  Either way, :func:`_iter_text_from_records` handles the
    shape normalization.

    Use this for cold-rebuild / consolidation passes that source records from
    multiple CLI adapters rather than file paths.
    """
    return _extract_from_text_stream(_iter_text_from_records(records))


# ---------------------------------------------------------------------------
# Incremental update (Stop-hook hot path)
# ---------------------------------------------------------------------------


# Fields that are "scalar identity" — supersede semantics (one value wins).
_SCALAR_FIELDS: tuple[str, ...] = (
    "name", "handle", "org", "email_primary", "github_user",
    "gitlab_host", "timezone", "billing_rail", "scm_policy",
    "default_cloud_provider",
)

# Fields that are unordered lists — append-with-dedupe semantics.
_LIST_FIELDS: tuple[str, ...] = (
    "emails_alt", "home_uplinks", "banned_providers",
    "philosophy", "own_products",
)


def _merge_lists(existing: list[Any] | None, new: list[Any]) -> list[Any]:
    """Union-merge two lists preserving existing order, appending novel items."""
    base = list(existing or [])
    seen = {str(x).lower() for x in base}
    for item in new:
        if str(item).lower() not in seen:
            base.append(item)
            seen.add(str(item).lower())
    return base


def _merge_scalar(
    key: str,
    existing_val: Any,
    existing_conf: float,
    candidate_val: Any,
    candidate_conf: float,
    candidate_evidence: int,
    tentative: dict[str, list[dict[str, Any]]],
) -> tuple[Any, float, bool]:
    """Apply append-supersede rules for one scalar field.

    Returns ``(value, confidence, superseded)``. Rules (per R2 spec):
      - If the candidate equals the existing value → reassertion bump.
      - If the existing is empty → take the candidate.
      - If contradicts → supersede only when ``candidate_evidence >= 2``
        OR ``candidate_conf > existing_conf``; otherwise stash the
        candidate in ``tentative[key]`` and keep the existing value.
    """
    if candidate_val in ("", None, [], {}):
        return existing_val, existing_conf, False

    if not existing_val:
        return candidate_val, candidate_conf, False

    if str(existing_val).strip().lower() == str(candidate_val).strip().lower():
        # Reassertion — small confidence bump, no value change.
        new_conf = min(1.0, float(existing_conf) + 0.05)
        return existing_val, new_conf, False

    # Contradiction: needs corroboration to overwrite.
    if candidate_evidence >= 2 or candidate_conf > existing_conf:
        return candidate_val, candidate_conf, True

    # Weak conflict — stash as tentative.
    tentative.setdefault(key, []).append(
        {
            "value": candidate_val,
            "confidence": candidate_conf,
            "evidence_count": candidate_evidence,
        }
    )
    return existing_val, existing_conf, False


def extract_incremental(
    records: Iterable[Any],
    existing_profile: OperatorProfile | None = None,
) -> OperatorProfile:
    """Mine *only the new* records and fold them into ``existing_profile``.

    Idempotent: running this twice with the same records yields the same
    merged profile (reassertion-bump saturates at confidence=1.0, list
    unions are set-semantic).

    Per R2 research, append-supersede applies:

    * Scalar identity fields (``name``, ``email_primary``, ...) are only
      overwritten when the new evidence has either ``>=2`` corroborations
      in this batch, or strictly higher confidence than the stored value.
      Weak contradictions are routed to a ``tentative_facts`` bucket
      (stored as ``_tentative.<key>`` JSON in :mod:`index.operator`).
    * List fields (``philosophy``, ``banned_providers``, ...) are union-
      merged with case-insensitive dedupe.
    """
    base = existing_profile or OperatorProfile()
    candidate = _extract_from_text_stream(_iter_text_from_records(records))

    # Snapshot evidence counts per scalar field from the candidate's sources
    # (number of citations ~= number of distinct evidence sites).
    def _evidence(key: str) -> int:
        return len(candidate.sources.get(key, []))

    tentative: dict[str, list[dict[str, Any]]] = {}
    merged = OperatorProfile()
    # Start from the existing values, then layer the candidate.
    for key in base.to_field_dict().keys():
        setattr(merged, key, getattr(base, key))
    merged.confidence = dict(base.confidence)
    merged.sources = {k: list(v) for k, v in base.sources.items()}

    # Scalars
    for key in _SCALAR_FIELDS:
        cand_val = getattr(candidate, key, "")
        if not cand_val:
            continue
        existing_val = getattr(base, key, "")
        existing_conf = base.confidence.get(key, 0.0) if existing_val else 0.0
        cand_conf = candidate.confidence.get(key, 0.5)
        cand_evidence = _evidence(key)
        new_val, new_conf, _ = _merge_scalar(
            key,
            existing_val,
            existing_conf,
            cand_val,
            cand_conf,
            cand_evidence,
            tentative,
        )
        setattr(merged, key, new_val)
        if new_val:
            merged.confidence[key] = new_conf
        # Append new citations regardless.
        if candidate.sources.get(key):
            bucket = merged.sources.setdefault(key, [])
            for cite in candidate.sources[key]:
                if cite not in bucket and len(bucket) < 10:
                    bucket.append(cite)

    # Lists
    for key in _LIST_FIELDS:
        cand_list = list(getattr(candidate, key, []) or [])
        if not cand_list:
            continue
        existing_list = list(getattr(base, key, []) or [])
        merged_list = _merge_lists(existing_list, cand_list)
        setattr(merged, key, merged_list)
        if merged_list:
            existing_conf = base.confidence.get(key, 0.4)
            cand_conf = candidate.confidence.get(key, 0.5)
            merged.confidence[key] = min(1.0, max(existing_conf, cand_conf) + 0.02)
        if candidate.sources.get(key):
            bucket = merged.sources.setdefault(key, [])
            for cite in candidate.sources[key]:
                if cite not in bucket and len(bucket) < 10:
                    bucket.append(cite)

    # Machines dict — merge per-host with hit-counter accumulation.
    if candidate.machines:
        base_machines = dict(merged.machines or {})
        for host, slot in candidate.machines.items():
            prior = base_machines.get(host, {})
            base_machines[host] = {
                "role": prior.get("role") or slot.get("role", ""),
                "ip": prior.get("ip") or slot.get("ip", ""),
                "tailscale": prior.get("tailscale") or slot.get("tailscale", ""),
                "hits": int(prior.get("hits", 0)) + int(slot.get("hits", 0)),
            }
        merged.machines = base_machines
        merged.confidence["machines"] = min(
            1.0,
            base.confidence.get("machines", 0.4)
            + 0.02 * sum(s.get("hits", 0) for s in candidate.machines.values()),
        )

    # Stash tentative facts under sources["_tentative"] so persist can route
    # them to the storage-layer tentative bucket without a schema change.
    if tentative:
        merged.sources["_tentative"] = [
            f"{k}={json.dumps(v, ensure_ascii=False, sort_keys=True)}"
            for k, v in tentative.items()
        ]

    return merged


def persist_incremental_profile(
    conn, profile: OperatorProfile
) -> int:
    """Persist ``profile`` plus any ``_tentative`` candidates.

    Tentative candidates land under the reserved ``_tentative.<field>``
    key in ``operator_profile`` so we don't need a new table. Promotion
    to a real value is done by the next ``extract_incremental`` run that
    sees corroborating evidence.
    """
    from index.operator import upsert_profile_field

    written = persist_profile(conn, profile)

    from index.operator import get_profile_field

    tent_cites = profile.sources.get("_tentative", [])
    for cite in tent_cites:
        if "=" not in cite:
            continue
        key, raw = cite.split("=", 1)
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        # `decoded` is the list-of-candidate-dicts encoded by
        # extract_incremental. Merge it onto whatever's already stored
        # for this tentative slot.
        if not isinstance(decoded, list):
            decoded = [decoded]
        tkey = f"_tentative.{key}"
        prior = get_profile_field(conn, tkey)
        prior_list = prior[0] if (prior and isinstance(prior[0], list)) else []
        merged_list = list(prior_list) + list(decoded)
        top_conf = max(
            (float(d.get("confidence", 0.5)) for d in merged_list if isinstance(d, dict)),
            default=0.5,
        )
        upsert_profile_field(
            conn,
            key=tkey,
            value=merged_list,
            confidence=top_conf,
            sources=[],
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_profile(conn, profile: OperatorProfile) -> int:
    """Write ``profile`` into the ``operator_profile`` table.

    Returns the number of fields written. Empty / default fields are
    skipped — we don't want to overwrite a previously-mined high-confidence
    value with an empty string from a barren run.
    """
    # Local import avoids a circular dep when ``index.operator`` itself
    # imports from this module (it currently does not, but be defensive).
    from index.operator import upsert_profile_field

    written = 0
    field_values = profile.to_field_dict()
    for key, value in field_values.items():
        # Treat empty string / empty list / empty dict as "no signal" — keep
        # whatever was previously stored.
        if value == "" or value == [] or value == {}:
            continue
        confidence = profile.confidence.get(key, 0.5)
        sources = profile.sources.get(key, [])
        upsert_profile_field(
            conn,
            key=key,
            value=value,
            confidence=confidence,
            sources=sources,
        )
        written += 1
    return written
