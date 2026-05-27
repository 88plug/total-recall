"""Secret scrubber — runs over every extraction's textual fields before emit.

The whole point of `total-recall` is to index transcripts that contain real
operational content (env vars, tokens, paths, etc.). We never want a
high-confidence "decision" extraction to carry a Bearer token forward into the
recall index. Scrub aggressively; false positives are far cheaper than leaks.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


# Order matters: longer/more-specific patterns first so they win over the
# generic `password=...` catch-alls.
#
# Some entries carry a custom replacement string: the PEM block keeps the
# readable BEGIN marker, and the URL basic-auth scrubber uses back-references
# so we only redact the password segment (preserving scheme/host for debug).
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # JWT (header.payload.sig) — three base64url segments separated by dots,
    # always starting `eyJ` (`{"...`).
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED]"),
    # PEM private key block (multi-line). Run before any generic kv catch-all.
    (
        "pem_private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
        "-----BEGIN [REDACTED PRIVATE KEY]-----",
    ),
    # Anthropic-style API keys.
    ("anthropic_sk", re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    # AWS access keys.
    ("aws_akia", re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    # GitHub personal access tokens (classic).
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{30,}"), "[REDACTED]"),
    # GitHub non-classic tokens: ghs_ (server), gho_ (oauth), ghu_ (user-to-server), ghr_ (refresh).
    ("github_non_classic", re.compile(r"gh[sour]_[A-Za-z0-9]{30,}"), "[REDACTED]"),
    # GitLab personal access tokens.
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    # npm publish tokens.
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{36}"), "[REDACTED]"),
    # Google API keys.
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED]"),
    # Slack tokens (broader): xoxb / xoxp / xoxo / xoxa / xoxr.
    ("slack_xox", re.compile(r"xox[bpoar]-[A-Za-z0-9-]+"), "[REDACTED]"),
    # `Authorization: Bearer ...` headers.
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"), "[REDACTED]"),
    # URL basic-auth password: keep scheme://user: ... @ intact, redact only the password.
    (
        "url_basic_auth",
        re.compile(r"(\w+://[^/\s:@]+:)[^@/\s]+(@)"),
        r"\1[REDACTED]\2",
    ),
    # Generic `password = ...` / `password: ...`.
    ("password_kv", re.compile(r"(?i)password\s*[:=]\s*\S+"), "[REDACTED]"),
    # Generic `api_key = ...` / `api-key: ...`.
    ("api_key_kv", re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"), "[REDACTED]"),
    # Generic `secret = ...` / `token: ...` / `private_key: ...` catch-all.
    (
        "secret_kv",
        re.compile(r"(?i)(secret|token|private[_-]?key)\s*[:=]\s*\S+"),
        "[REDACTED]",
    ),
]


def scrub_secrets(text: str) -> str:
    """Replace every matched secret pattern in `text` with `[REDACTED]`.

    Non-string input is returned unchanged — callers that hand us None / bytes
    are buggy but we'd rather not crash an extractor pipeline over it.
    """
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    for label, pat, repl in _SECRET_PATTERNS:
        new = pat.sub(repl, redacted)
        if new != redacted:
            log.debug("scrubbed pattern=%s in extraction text", label)
            redacted = new
    return redacted


__all__ = ["scrub_secrets"]
