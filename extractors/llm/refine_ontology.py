"""Local-LLM refinement for ontology vocabulary definitions and project narratives.

Off by default. If ``get_default_client()`` returns an unavailable client (no
ollama / llama.cpp reachable) both public functions are identity transforms.

Design constraints (matches the package):
- Local-only — operator transcripts never leave the machine.
- Graceful absence — any LLM failure returns the input unchanged.
- Cold-path only — not called from Stop-hook hot path.
- Batched — at most 25 items per LLM request to stay within small-model context.
- Anti-hallucination — returned items are validated against the input set;
  unknown items are dropped and overlong text (>300 chars) is rejected.
- Anti-echo — output that parrots the input context is suppressed (set to None).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from extractors.llm.client import LLMClient

log = logging.getLogger(__name__)

__all__ = [
    "refine_vocabulary_definitions",
    "refine_project_narratives",
    "_is_echo",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BATCH_SIZE: int = 25
_MAX_DEF_LEN: int = 300
_ECHO_THRESHOLD: float = 0.75
# Minimum chars required in both strings before the verbatim-substring check fires.
# Short sources (e.g. a single letter "z") would otherwise match any word that starts
# with that letter, causing false positives.
_VERBATIM_MIN_LEN: int = 10

_VOCAB_SYSTEM_PROMPT = """\
You are a terminology extractor. Your job is to write a SHORT definition of what \
a term means in THIS operator's context.

RULES:
1. Do NOT copy, quote, or repeat the context text. Restate the meaning in your OWN words as one short sentence.
2. Use only the information in the provided snippet — do not add general knowledge.
3. If the snippet is too vague, ambiguous, or off-topic to form a confident definition, return null for that term.

EXAMPLES:
  term: "sharechain"
  snippet: "the sharechain links p2pool shares together so payouts are verifiable"
  → definition: "A linked sequence of p2pool shares that enables verifiable miner payouts."

  term: "fan-out"
  snippet: "fan-out parallel agents research wave then build wave non-overlapping files"
  → definition: "A workflow pattern where multiple agents work in parallel on separate subtasks before merging results."

  term: "xyzzy"
  snippet: "xyzzy"
  → definition: null  (snippet gives no usable information)

Output JSON only — no prose before or after:
{"definitions": [{"term": "<str>", "definition": "<str>|null"}, ...]}\
"""

_NARRATIVE_SYSTEM_PROMPT = """\
You are a project summariser. Your job is to write a SHORT description of what a \
project does based on the provided context snippets.

RULES:
1. Do NOT copy, quote, or repeat text from the snippets. Write in your OWN words as 1-2 sentences.
2. Only describe capabilities that are clearly evidenced in the snippets — do not invent features.
3. If the snippets are too sparse or unclear, return null for that project.

EXAMPLES:
  cwd: "ip-service-for-docker"
  snippets: ["returns the real client IP even behind NAT", "tiny Go HTTP service", "used by sidecar relay fleet"]
  → narrative: "A lightweight Go HTTP service that reports the real client IP address, used by the sidecar relay infrastructure."

  cwd: "temp-scratch"
  snippets: ["scratch", "testing stuff"]
  → narrative: null  (insufficient information to describe the project)

Output JSON only — no prose before or after:
{"narratives": [{"cwd": "<str>", "narrative": "<str>|null"}, ...]}\
"""

_VOCAB_SCHEMA = {
    "type": "object",
    "properties": {
        "definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": ["string", "null"]},
                },
                "required": ["term", "definition"],
            },
        }
    },
    "required": ["definitions"],
}

_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "narratives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string"},
                    "narrative": {"type": ["string", "null"]},
                },
                "required": ["cwd", "narrative"],
            },
        }
    },
    "required": ["narratives"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase and split on non-alphanumeric; drop tokens shorter than 3 chars."""
    return {tok for tok in _TOKEN_RE.split(text.lower()) if len(tok) >= 3}


def _is_echo(output: str, source: str, threshold: float = _ECHO_THRESHOLD) -> bool:
    """Return True if *output* is essentially a copy of *source*.

    Two signals:
    - Verbatim substring: output is contained in source (or vice versa).
    - Token-level containment: overlap = |out_tokens ∩ src_tokens| / max(1, |out_tokens|)
      >= threshold.  A high ratio means most of what the model wrote came straight
      from the source text — i.e. it echoed rather than synthesised.
    """
    if not output or not source:
        return False
    out_lower = output.lower().strip()
    src_lower = source.lower().strip()
    # Only apply verbatim check when both strings are long enough to avoid
    # false positives from single-char / very short sources.
    if len(out_lower) >= _VERBATIM_MIN_LEN and len(src_lower) >= _VERBATIM_MIN_LEN:
        if out_lower in src_lower or src_lower in out_lower:
            return True
    out_tokens = _tokenize(output)
    src_tokens = _tokenize(source)
    overlap = len(out_tokens & src_tokens) / max(1, len(out_tokens))
    return overlap >= threshold


def _get_client(client: "LLMClient | None") -> "LLMClient | None":
    """Return a usable client or None."""
    if client is not None:
        return client if client.available else None
    try:
        from extractors.llm.client import get_default_client

        c = get_default_client()
        return c if c.available else None
    except ImportError:
        return None


def _truncate_or_none(text: str | None) -> str | None:
    """Return text only if within length budget, else None."""
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) > _MAX_DEF_LEN:
        return None
    return stripped


def _build_vocab_user_prompt(batch: list[dict]) -> str:
    lines = []
    for item in batch:
        term = item.get("term", "")
        snippet = item.get("context_snippet") or ""
        cat = item.get("category") or ""
        freq = item.get("frequency", 0)
        meta = f"category={cat}, frequency={freq}" if cat else f"frequency={freq}"
        lines.append(f'- term: "{term}" ({meta})\n  snippet: "{snippet}"')
    return "Terms to define:\n\n" + "\n\n".join(lines)


def _build_narrative_user_prompt(batch: list[dict]) -> str:
    lines = []
    for item in batch:
        cwd = item.get("cwd", "")
        display = item.get("display_name") or cwd
        snippets = item.get("context_snippets") or []
        purpose = item.get("purpose") or ""
        lang = item.get("primary_language") or ""
        related = item.get("related_projects") or []
        related_names = ", ".join(
            r.get("display_name", r.get("cwd", "")) if isinstance(r, dict) else str(r)
            for r in related[:5]
        )
        snippet_block = "\n  ".join(f'"{s}"' for s in snippets[:5]) if snippets else '""'
        meta = f"display_name={display!r}"
        if lang:
            meta += f", primary_language={lang}"
        if related_names:
            meta += f", related={related_names}"
        if purpose:
            meta += f", first_prompt={purpose[:80]!r}"
        lines.append(f"- cwd: {cwd!r} ({meta})\n  snippets:\n  {snippet_block}")
    return "Projects to describe:\n\n" + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def refine_vocabulary_definitions(
    terms: list[dict],
    client: "LLMClient | None" = None,
) -> list[dict]:
    """For each term, return a copy with a ``definition`` field added/updated.

    The LLM is asked to produce ONE concise sentence per term, grounded in the
    provided context snippet (no general-knowledge hallucination). If the
    snippet is too sparse, the definition is set to None (preserved as-is if
    the term already had one).

    Args:
        terms: List of dicts with keys ``term``, ``frequency``, ``category``
            (optional), ``context_snippet`` (optional).
        client: An :class:`~extractors.llm.client.LLMClient` instance, or
            ``None`` to auto-resolve via :func:`get_default_client`.

    Returns:
        A list of the same length as ``terms``. Each item is a shallow copy of
        the corresponding input dict, possibly with ``definition`` updated.
        Input order is preserved. No new items are added.
    """
    if not terms:
        return []

    active = _get_client(client)
    if active is None:
        return [dict(t) for t in terms]

    # Index input by term (lower-cased) for O(1) lookup.
    input_index: dict[str, dict] = {t["term"].lower(): t for t in terms}
    result_map: dict[str, str | None] = {}  # term_lower → definition

    for batch_start in range(0, len(terms), _BATCH_SIZE):
        batch = terms[batch_start : batch_start + _BATCH_SIZE]
        user_prompt = _build_vocab_user_prompt(batch)

        try:
            raw = active.generate_json(
                system=_VOCAB_SYSTEM_PROMPT,
                user=user_prompt,
                schema=_VOCAB_SCHEMA,
            )
        except Exception:
            log.debug("LLM vocab call failed", exc_info=True)
            raw = None

        if raw is None:
            continue

        for entry in raw.get("definitions") or []:
            if not isinstance(entry, dict):
                continue
            returned_term = entry.get("term")
            if not isinstance(returned_term, str):
                continue
            key = returned_term.lower()
            if key not in input_index:
                # Anti-hallucination: drop terms not in the input set.
                log.debug("LLM returned unknown term %r — dropped", returned_term)
                continue
            defn = entry.get("definition")
            if isinstance(defn, str):
                defn = _truncate_or_none(defn)
            # Anti-echo: suppress definitions that are just the context parroted back.
            if isinstance(defn, str):
                snippet = input_index[key].get("context_snippet") or ""
                if _is_echo(defn, snippet):
                    log.debug("Echo detected for term %r — suppressing definition", returned_term)
                    defn = None
            result_map[key] = defn

    # Build output preserving input order.
    out: list[dict] = []
    for term_dict in terms:
        copy = dict(term_dict)
        key = term_dict["term"].lower()
        if key in result_map:
            copy["definition"] = result_map[key]
        out.append(copy)
    return out


def refine_project_narratives(
    projects: list[dict],
    client: "LLMClient | None" = None,
) -> list[dict]:
    """For each project, return a copy with a ``narrative`` field added.

    The narrative is 1-2 sentences grounded in the project's context snippets.
    Empty narrative on low confidence or absent LLM.

    Args:
        projects: List of dicts with keys ``cwd``, ``display_name``,
            ``purpose`` (optional), ``primary_language`` (optional),
            ``related_projects`` (optional list), ``context_snippets``
            (optional list[str]).
        client: An :class:`~extractors.llm.client.LLMClient` instance, or
            ``None`` to auto-resolve via :func:`get_default_client`.

    Returns:
        A list of the same length as ``projects``. Each item is a shallow copy
        of the corresponding input dict, possibly with ``narrative`` set.
        Input order is preserved. No new items are added.
    """
    if not projects:
        return []

    active = _get_client(client)
    if active is None:
        return [dict(p) for p in projects]

    input_index: dict[str, dict] = {p["cwd"]: p for p in projects}
    result_map: dict[str, str | None] = {}  # cwd → narrative

    for batch_start in range(0, len(projects), _BATCH_SIZE):
        batch = projects[batch_start : batch_start + _BATCH_SIZE]
        user_prompt = _build_narrative_user_prompt(batch)

        try:
            raw = active.generate_json(
                system=_NARRATIVE_SYSTEM_PROMPT,
                user=user_prompt,
                schema=_NARRATIVE_SCHEMA,
            )
        except Exception:
            log.debug("LLM narrative call failed", exc_info=True)
            raw = None

        if raw is None:
            continue

        for entry in raw.get("narratives") or []:
            if not isinstance(entry, dict):
                continue
            returned_cwd = entry.get("cwd")
            if not isinstance(returned_cwd, str):
                continue
            if returned_cwd not in input_index:
                # Anti-hallucination: drop projects not in the input set.
                log.debug("LLM returned unknown cwd %r — dropped", returned_cwd)
                continue
            narrative = entry.get("narrative")
            if isinstance(narrative, str):
                narrative = _truncate_or_none(narrative)
            # Anti-echo: suppress narratives that are just the snippets parroted back.
            if isinstance(narrative, str):
                joined_snippets = " ".join(
                    input_index[returned_cwd].get("context_snippets") or []
                )
                if _is_echo(narrative, joined_snippets):
                    log.debug("Echo detected for cwd %r — suppressing narrative", returned_cwd)
                    narrative = None
            result_map[returned_cwd] = narrative

    out: list[dict] = []
    for proj_dict in projects:
        copy = dict(proj_dict)
        cwd = proj_dict["cwd"]
        if cwd in result_map:
            copy["narrative"] = result_map[cwd]
        out.append(copy)
    return out
