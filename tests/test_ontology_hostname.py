"""Machine-name NER must not mistake cwd directory-slugs for hostnames.

Before v0.14.0 the hostname regex accepted any kebab-case token, so project
directory slugs like ``a-conversation-with-daniel-kahneman-about-noise`` and
``claude-code-session-logs-data-mining`` flooded the ``machines`` table (~61k
garbage rows). ``_is_cwd_slug`` now filters them while keeping real terse hosts.
"""
from __future__ import annotations

import pytest

from extractors.ontology import _extract_machines_from_text, _is_cwd_slug

REAL_HOSTS = [
    "relay-eu-west",
    "db-prod-01",
    "node-us-east-3",
    "racknerd-aa11",
    "mail.acme.example",
    "dev-box",
    "prod-db",
    "staging-web",
]

SLUGS = [
    "a-conversation-with-daniel-kahneman-about-noise",
    "claude-code-session-logs-data-mining",
    "home-andrew-myproject",
    "review-of-the-data-mining-project",
    "session-logs-data",
    "the-great-refactor-saga",
]


@pytest.mark.parametrize("host", REAL_HOSTS)
def test_real_hostnames_not_slugs(host: str) -> None:
    assert _is_cwd_slug(host) is False, f"{host} should NOT be flagged a slug"


@pytest.mark.parametrize("slug", SLUGS)
def test_cwd_slugs_flagged(slug: str) -> None:
    assert _is_cwd_slug(slug) is True, f"{slug} should be flagged a slug"


def test_extract_machines_excludes_slugs() -> None:
    """A blob mentioning a real host + slugs yields only the host."""
    text = (
        "deployed to relay-eu-west from the "
        "claude-code-session-logs-data-mining project after "
        "a-conversation-with-daniel-kahneman-about-noise"
    )
    machines: dict = {}
    _extract_machines_from_text(text, 1_700_000_000, machines)
    keys = set(machines)
    assert "relay-eu-west" in keys
    assert "claude-code-session-logs-data-mining" not in keys
    assert "a-conversation-with-daniel-kahneman-about-noise" not in keys
