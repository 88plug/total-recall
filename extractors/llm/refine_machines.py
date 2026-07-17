"""LLM refinement pass for the operator-profile machines dict.

The heuristic extractor casts a wide net and picks up noise tokens
(``brute-force``, ``hardening``, ``cloudflare``, ``timeouts``, …) alongside
real hostnames.  This module runs a single local-LLM call to classify each
candidate and returns only the entries the model judged to be real machine
names — preserving the original schema intact.

Design constraints
------------------
- **Off by default / graceful absence** — if the LLM client is unavailable or
  returns an error the function returns *heuristic_machines* unchanged.
- **Local-only** — transcripts never leave the machine; only the dict *keys*
  (hostnames), hit counts, and optional context snippets are sent.
- **Subset-only guard** — hallucinated names (not present in the input dict)
  are silently dropped before the result is returned.
- **Batching** — if the input has >200 entries the LLM is called in chunks of
  100 so we never blow the context window; results are merged by union of all
  ``keep`` lists.
- **Cold-path only** — do not call from the Stop-hook hot path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from extractors.llm.client import LLMClient

log = logging.getLogger(__name__)

# Maximum entries per LLM call (guards context-window limits).
_CHUNK_SIZE = 100

# JSON schema for structured output.
_KEEP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "keep": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Subset of input keys that are real machine hostnames. "
                "Must not contain any key not present in the input."
            ),
        }
    },
    "required": ["keep"],
}

_SYSTEM_PROMPT = """\
You are a hostname classifier. You will be given a list of tokens that were \
extracted from shell-command transcripts as potential machine hostnames. Your \
job is to decide which tokens are REAL machine hostnames (server names, VPS \
hostnames, relay nodes, workstation names, NAS boxes, VM names, container \
names, etc.) and which are NOISE (ordinary English words, product names, \
vendor names, error messages, concepts, adjectives, gerunds, or any other \
non-hostname token).

Respond ONLY with a JSON object matching this schema:
  {"keep": ["host1", "host2", ...]}

Rules:
- "keep" must be a STRICT SUBSET of the input keys — do NOT invent new names.
- Include a token only when you are confident it is a real machine identifier.
- Tokens that look like English words describing actions or concepts \
  (e.g. "hardening", "brute-force", "timeouts", "cloudflare", "add-on") \
  should be EXCLUDED.
- Tokens with typical hostname patterns (hyphens between words, datacenter \
  abbreviations, numeric suffixes, relay/node/srv/vps/box/host in the name, \
  subdomain-style labels) are more likely to be real machines.
- A bare single word (no dot, no hyphen) CAN still be a real hostname if the \
  context shows it used as a machine: e.g. "ssh <name>", "on <name>", \
  "deploy to <name>", "<name> is up/down/rebooted". KEEP it when context \
  supports it; DROP it only when it is clearly a common English word with no \
  machine context.
- When in doubt, EXCLUDE.
"""


def _build_user_prompt(
    chunk: dict[str, dict],
    operator_email: str | None,
    sample_contexts: dict[str, list[str]] | None,
) -> str:
    lines: list[str] = []

    if operator_email:
        # Provide domain context only — avoid logging full email in prompts.
        domain = operator_email.split("@", 1)[-1] if "@" in operator_email else operator_email
        lines.append(f"Operator's email domain: {domain}")
        lines.append("")

    lines.append("Candidate tokens (key → hit count):")
    for host, info in chunk.items():
        hits = info.get("hits", "?")
        line = f"  {host!r}: {hits} hit(s)"
        if sample_contexts:
            snippets = sample_contexts.get(host, [])[:3]
            if snippets:
                joined = " | ".join(s.strip() for s in snippets)
                line += f"  [context: {joined}]"
        lines.append(line)

    lines.append("")
    lines.append('Return JSON: {"keep": [<tokens that are REAL hostnames from the list above>]}')
    return "\n".join(lines)


def _call_llm_for_chunk(
    chunk: dict[str, dict],
    operator_email: str | None,
    sample_contexts: dict[str, list[str]] | None,
    client: LLMClient,
) -> set[str]:
    """Ask the LLM to classify one chunk; return the set of kept keys."""
    user_prompt = _build_user_prompt(chunk, operator_email, sample_contexts)
    result = client.generate_json(
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        schema=_KEEP_SCHEMA,
    )
    if result is None:
        log.debug("refine_machines: LLM returned None for chunk of %d entries", len(chunk))
        return set(chunk.keys())  # fall back to keeping all in this chunk

    keep_raw = result.get("keep")
    if not isinstance(keep_raw, list):
        log.debug("refine_machines: unexpected LLM response shape: %r", result)
        return set(chunk.keys())

    # Subset-of-input guard: drop non-strings (e.g. numbers, nulls from
    # malformed model output) then drop anything not in the original chunk keys.
    valid = {k for k in keep_raw if isinstance(k, str) and k in chunk}
    hallucinated = {k for k in keep_raw if isinstance(k, str)} - valid
    if hallucinated:
        log.debug(
            "refine_machines: dropped %d hallucinated key(s): %s",
            len(hallucinated),
            sorted(hallucinated),
        )
    return valid


def refine_machines(
    heuristic_machines: dict[str, dict],
    operator_email: str | None = None,
    client: LLMClient | None = None,
    sample_contexts: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """Return a filtered/refined machines dict.

    Parameters
    ----------
    heuristic_machines:
        The ``operator_profile.machines`` dict in its current shape::

            {hostname: {"role": str|None, "ip": str|None,
                        "tailscale": bool, "hits": int}}

    operator_email:
        Optional operator email for disambiguation context.  Only the domain
        part is included in the prompt; the full address is never logged.

    client:
        An :class:`extractors.llm.client.LLMClient` instance.  Defaults to
        ``get_default_client()``.

    sample_contexts:
        Optional mapping of ``{host: [up-to-3 surrounding text snippets]}``
        to give the model richer signal for ambiguous tokens.

    Returns
    -------
    dict[str, dict]
        A *subset* of ``heuristic_machines`` with the same per-entry schema.
        Returns ``heuristic_machines`` unchanged if the client is unavailable
        or any error occurs.
    """
    if not heuristic_machines:
        return heuristic_machines

    # Lazy import so the module is importable even before the client exists.
    if client is None:
        from extractors.llm.client import get_default_client

        client = get_default_client()

    if not client.available:
        log.debug("refine_machines: LLM client unavailable — returning heuristics unchanged")
        return heuristic_machines

    all_keys = list(heuristic_machines.keys())

    try:
        if len(all_keys) <= _CHUNK_SIZE:
            # Single call — common case.
            kept_keys = _call_llm_for_chunk(
                heuristic_machines, operator_email, sample_contexts, client
            )
        else:
            # Batch in chunks; union of all kept sets.
            kept_keys: set[str] = set()
            for i in range(0, len(all_keys), _CHUNK_SIZE):
                chunk_keys = all_keys[i : i + _CHUNK_SIZE]
                chunk = {k: heuristic_machines[k] for k in chunk_keys}
                kept_keys |= _call_llm_for_chunk(chunk, operator_email, sample_contexts, client)

        # Preserve original schema; return only kept entries.
        return {k: heuristic_machines[k] for k in all_keys if k in kept_keys}

    except Exception:
        log.exception("refine_machines: unexpected error — returning heuristics unchanged")
        return heuristic_machines
