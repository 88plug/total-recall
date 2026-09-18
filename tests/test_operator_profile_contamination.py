"""Regression tests: the operator profile must be built from the operator.

Identity is a claim only the operator can make about themselves, but the
extractor read every record — assistant replies, tool results, pasted diffs,
command output. On a real 3M-message corpus that produced:

    name         "The Monitor"   <- an assistant sentence about the Monitor tool
    handle       "torch"         <- a package name in command output
    github_user  "habanaai"      <- an upstream org quoted from a URL

Filtering to operator turns is necessary but not sufficient: ranking by raw
frequency still picks whatever the corpus is *about*. A transcript on Intel
Gaudi says "Intel Gaudi" 438x against the operator's own name 341x, and cites
`github.com/vllm-project` 260x against the operator's account 39x. Candidates
are therefore ranked by corroboration against the one thing already known for
certain — the operator's email — with frequency as the tiebreak only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extractors.operator_profile import (  # noqa: E402
    _extract_from_text_stream,
    _host_is_english_compound,
    _identity_tokens_from_email,
    _is_noise_token,
    _looks_like_product_name,
    _shares_identity_token,
    extract_operator_profile_from_records,
)


def _stream(*texts: str):
    for i, t in enumerate(texts):
        yield t, "<test>", i


# ---------------------------------------------------------------------------
# Identity corroboration
# ---------------------------------------------------------------------------


def test_assistant_text_cannot_set_the_name() -> None:
    """End-to-end on the exact shape that produced name='The Monitor'."""
    profile = extract_operator_profile_from_records(
        [
            {"type": "assistant", "text": "I'll wait on the Monitor for readiness."},
            {"type": "assistant", "text": "Now I wait for the Monitor readiness event."},
            {"type": "user", "content_kind": "string", "text": "my email is dana@example.com"},
        ]
    )
    assert profile.name != "The Monitor"
    assert profile.name == "Dana", "should fall back to the email local part"


def test_identity_tokens_from_email() -> None:
    assert _identity_tokens_from_email("andrew@88plug.com") == {"andrew", "88plug"}
    assert _identity_tokens_from_email("dana.m@example.co.uk") == {
        "dana.m",
        "dana",
        "m",
        "co",
    }
    assert _identity_tokens_from_email(None) == set()
    assert _identity_tokens_from_email("not-an-email") == set()


def test_shares_identity_token_matches_any_word() -> None:
    toks = {"andrew", "88plug"}
    assert _shares_identity_token("Andrew Mello", toks) is True
    assert _shares_identity_token("88plug", toks) is True
    assert _shares_identity_token("Intel Gaudi", toks) is False
    assert _shares_identity_token("", toks) is False


def test_corroborated_name_beats_a_more_frequent_product_name() -> None:
    """ "Intel Gaudi" 3x vs "Andrew Mello" 1x — the email breaks the tie."""
    texts = ["reach me at andrew@88plug.com"]
    texts += ["Intel Gaudi throughput is the target"] * 3
    texts += ["Andrew Mello owns this repo"]
    profile = _extract_from_text_stream(_stream(*texts))
    assert profile.name == "Andrew Mello"


def test_quoted_code_cannot_outrank_the_real_author() -> None:
    """`author = "..."` also matches inside a pasted diff, tying at 1 hit."""
    profile = _extract_from_text_stream(
        _stream(
            "contact andrew@88plug.com",
            'the newer Intel-realigned tree has author="Intel" in it',
            'our setup.py says author = "Andrew Mello"',
        )
    )
    assert profile.name == "Andrew Mello"


def test_github_user_prefers_the_operators_own_account() -> None:
    """Upstream repos are cited far more often than the operator's own."""
    texts = ["mail: andrew@88plug.com"]
    texts += ["see github.com/vllm-project/vllm for the kernel"] * 5
    texts += ["pushed to github.com/88plug/total-recall"]
    profile = _extract_from_text_stream(_stream(*texts))
    assert profile.github_user == "88plug"


def test_frequency_still_decides_without_corroboration() -> None:
    """No email to corroborate against -> fall back to raw frequency."""
    texts = ["see github.com/vllm-project/vllm"] * 3 + ["see github.com/someone/x"]
    profile = _extract_from_text_stream(_stream(*texts))
    assert profile.github_user == "vllm-project"


# ---------------------------------------------------------------------------
# Noise filtering
# ---------------------------------------------------------------------------


def test_is_noise_token() -> None:
    for junk in ("a", "the", "this", "that", "on", "word", "generic", "bash", "361", ""):
        assert _is_noise_token(junk) is True, junk
    for real in ("gaudi", "harvest-intel", "postgres", "88plug"):
        assert _is_noise_token(real) is False, real


def test_banned_providers_drops_common_words() -> None:
    profile = _extract_from_text_stream(
        _stream(
            "never use the word again",
            "never use runpod again",
        )
    )
    assert "runpod" in profile.banned_providers
    for junk in ("the", "word", "a", "this"):
        assert junk not in profile.banned_providers


def test_home_uplinks_collapse_case_variants() -> None:
    """ "wave" and "Wave" ranked as two separate uplinks."""
    profile = _extract_from_text_stream(
        _stream("my home internet is Wave", "the wave uplink is flaky")
    )
    uplinks = profile.home_uplinks
    assert len(uplinks) == len({u.lower() for u in uplinks})


# ---------------------------------------------------------------------------
# Hostname shape rules
# ---------------------------------------------------------------------------


def test_english_prose_in_host_position_is_rejected() -> None:
    """`ssh connections` / `deploying to production` were recorded as machines.

    The old rule admitted any bare word >= 8 chars that a hand-written list
    did not happen to contain, so open-ended English always leaked through.
    """
    profile = _extract_from_text_stream(
        _stream(
            "ssh connections were dropping all morning",
            "deploying to production later today",
            "hostname: reachability is the open question",
            "check the credentials and infrastructure",
        )
    )
    assert profile.machines == {}


def test_host_shaped_tokens_are_kept() -> None:
    profile = _extract_from_text_stream(
        _stream(
            "ssh gaudi-1 and check the fans",
            "deploying to edge-01 now",
            "hostname: gw.example.com",
            "ssh host01 for the logs",
        )
    )
    assert set(profile.machines) == {"gaudi-1", "edge-01", "gw.example.com", "host01"}


def test_bare_name_kept_when_corpus_has_a_shaped_sibling() -> None:
    """`yuzu` is a real host; the old rule dropped it for being short.

    It is admitted because the same corpus contains `yuzu01`, so the family
    is evidenced rather than guessed.
    """
    profile = _extract_from_text_stream(
        _stream("ssh yuzu for the model dir", "ssh yuzu01 to compare")
    )
    assert "yuzu" in profile.machines
    assert "yuzu01" in profile.machines


def test_bare_name_dropped_without_a_shaped_sibling() -> None:
    profile = _extract_from_text_stream(_stream(*(["ssh operator to fix it"] * 3)))
    assert "operator" not in profile.machines


def test_hyphenated_english_phrases_are_not_hosts() -> None:
    """`read-only`, `key-based` and `off-subnet` have hostname punctuation."""
    profile = _extract_from_text_stream(
        _stream(
            "deploying to read-only mode",
            "ssh key-based auth is on",
            "hostname: off-subnet traffic",
        )
    )
    assert profile.machines == {}


def test_host_english_compound_helper() -> None:
    assert _host_is_english_compound("read-only") is True
    assert _host_is_english_compound("key-based") is True
    assert _host_is_english_compound("gaudi-1") is False, "has a digit"
    assert _host_is_english_compound("harvest-edge-01") is False
    assert _host_is_english_compound("yuzu") is False, "no hyphen"


# ---------------------------------------------------------------------------
# Product-name shape rules
# ---------------------------------------------------------------------------


def test_modifiers_are_not_product_names() -> None:
    for junk in ("own", "new", "existing", "current", "entire", "local", "active"):
        assert _looks_like_product_name(junk) is False, junk


def test_participles_are_not_product_names() -> None:
    """ "our edited compose project" named the edit, not the product."""
    for junk in ("edited", "reconstructed", "converted", "generated"):
        assert _looks_like_product_name(junk) is False, junk


def test_real_product_names_survive() -> None:
    for real in ("harvest-intel", "fgpu_ansible", "nixl", "sglang", "total-recall"):
        assert _looks_like_product_name(real) is True, real


def test_own_products_drops_the_captured_modifier() -> None:
    profile = _extract_from_text_stream(
        _stream(
            "our new project is shaping up",
            "my existing tool needs work",
            "our harvest-intel project ships today",
        )
    )
    assert "harvest-intel" in profile.own_products
    for junk in ("new", "existing"):
        assert junk not in profile.own_products
