"""Tests for extractors/llm/refine_ontology.py.

All tests are hermetic — no real ollama, no real network. The LLM client is
always mocked via ``unittest.mock.MagicMock`` or ``pytest.importorskip``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Skip the entire module gracefully if the L1 client hasn't been built yet.
refine_ontology = pytest.importorskip(
    "extractors.llm.refine_ontology",
    reason="extractors.llm.refine_ontology not yet present",
)

refine_vocabulary_definitions = refine_ontology.refine_vocabulary_definitions
refine_project_narratives = refine_ontology.refine_project_narratives
_is_echo = refine_ontology._is_echo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unavailable_client() -> MagicMock:
    """A mock LLMClient that reports itself as unavailable."""
    c = MagicMock()
    c.available = False
    return c


def _client_returning(payload: dict | None) -> MagicMock:
    """A mock LLMClient that returns ``payload`` from generate_json."""
    c = MagicMock()
    c.available = True
    c.generate_json.return_value = payload
    return c


def _sample_terms(n: int = 3) -> list[dict]:
    return [
        {
            "term": f"term{i}",
            "frequency": i + 1,
            "category": "concept",
            "context_snippet": f"The operator uses term{i} to mean thing {i}.",
        }
        for i in range(n)
    ]


def _sample_projects(n: int = 2) -> list[dict]:
    return [
        {
            "cwd": f"proj{i}",
            "display_name": f"Project {i}",
            "purpose": f"Does thing {i}",
            "primary_language": "python",
            "related_projects": [],
            "context_snippets": [f"Snippet about project {i}."],
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# refine_vocabulary_definitions
# ---------------------------------------------------------------------------


class TestRefineVocabularyDefinitions:
    def test_empty_input_returns_empty(self):
        client = _client_returning({"definitions": []})
        result = refine_vocabulary_definitions([], client=client)
        assert result == []

    def test_unavailable_client_returns_input_unchanged(self):
        terms = _sample_terms(2)
        result = refine_vocabulary_definitions(terms, client=_unavailable_client())
        assert result == [dict(t) for t in terms]
        # Ensure it's copies, not the same objects.
        assert result is not terms

    def test_none_client_falls_back_to_get_default_unavailable(self):
        """When client=None and get_default_client() is unavailable, input unchanged."""
        terms = _sample_terms(2)
        mock_client = _unavailable_client()
        with patch(
            "extractors.llm.refine_ontology._get_client", return_value=None
        ):
            result = refine_vocabulary_definitions(terms, client=None)
        assert result == [dict(t) for t in terms]

    def test_sensible_llm_response_merged_correctly(self):
        terms = _sample_terms(3)
        payload = {
            "definitions": [
                {"term": "term0", "definition": "First definition."},
                {"term": "term1", "definition": "Second definition."},
                {"term": "term2", "definition": None},
            ]
        }
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)

        assert len(result) == 3
        assert result[0]["term"] == "term0"
        assert result[0]["definition"] == "First definition."
        assert result[1]["definition"] == "Second definition."
        assert result[2]["definition"] is None

    def test_unknown_term_from_llm_dropped(self):
        """LLM returns a term not in the input — anti-hallucination guard."""
        terms = [{"term": "alpha", "frequency": 5, "category": "concept", "context_snippet": "Alpha is X."}]
        payload = {
            "definitions": [
                {"term": "alpha", "definition": "Known def."},
                {"term": "invented_term", "definition": "Should be dropped."},
            ]
        }
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)

        assert len(result) == 1
        assert result[0]["term"] == "alpha"
        assert result[0]["definition"] == "Known def."

    def test_overlong_definition_rejected(self):
        """Definitions longer than 300 chars are rejected (set to None)."""
        terms = [{"term": "verbose", "frequency": 2, "category": "concept", "context_snippet": "context"}]
        long_def = "A" * 301
        payload = {"definitions": [{"term": "verbose", "definition": long_def}]}
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)

        assert len(result) == 1
        # Overlong text is treated as None (low confidence / model verbosity).
        assert result[0].get("definition") is None

    def test_llm_returns_none_payload_input_unchanged(self):
        """generate_json returning None → terms returned unchanged."""
        terms = _sample_terms(2)
        client = _client_returning(None)
        result = refine_vocabulary_definitions(terms, client=client)
        assert result == [dict(t) for t in terms]

    def test_input_order_preserved(self):
        """Output list must match input order regardless of LLM response order."""
        terms = [
            {"term": "zeta", "frequency": 1, "category": None, "context_snippet": "z"},
            {"term": "alpha", "frequency": 2, "category": None, "context_snippet": "a"},
        ]
        payload = {
            "definitions": [
                # LLM returns in reverse order
                {"term": "alpha", "definition": "Alpha def."},
                {"term": "zeta", "definition": "Zeta def."},
            ]
        }
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)
        assert result[0]["term"] == "zeta"
        assert result[0]["definition"] == "Zeta def."
        assert result[1]["term"] == "alpha"
        assert result[1]["definition"] == "Alpha def."

    def test_no_new_terms_added(self):
        """Output must be same length as input — no phantom entries."""
        terms = _sample_terms(2)
        payload = {
            "definitions": [
                {"term": "term0", "definition": "Def."},
                {"term": "term1", "definition": "Def."},
                {"term": "extra_phantom", "definition": "Should not appear."},
            ]
        }
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)
        assert len(result) == len(terms)

    def test_batching_large_input(self):
        """Input > 25 items triggers multiple generate_json calls."""
        terms = _sample_terms(30)
        # All definitions returned as "def X"
        def side_effect(system, user, schema=None):
            batch_terms = [
                line.split('"')[1]
                for line in user.splitlines()
                if line.strip().startswith('- term:')
            ]
            return {
                "definitions": [
                    {"term": t, "definition": f"def for {t}"}
                    for t in batch_terms
                ]
            }

        client = MagicMock()
        client.available = True
        client.generate_json.side_effect = side_effect

        result = refine_vocabulary_definitions(terms, client=client)
        assert len(result) == 30
        # Two batches: 25 + 5
        assert client.generate_json.call_count == 2

    def test_case_insensitive_term_matching(self):
        """LLM may return the term in a different case — should still match."""
        terms = [{"term": "FooBar", "frequency": 3, "category": "concept", "context_snippet": "FooBar does X."}]
        payload = {"definitions": [{"term": "foobar", "definition": "Lowercase match."}]}
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)
        assert result[0]["definition"] == "Lowercase match."

    def test_generate_json_exception_returns_input_unchanged(self):
        """If generate_json raises, the function returns input unchanged."""
        terms = _sample_terms(2)
        client = MagicMock()
        client.available = True
        client.generate_json.side_effect = RuntimeError("connection refused")
        result = refine_vocabulary_definitions(terms, client=client)
        assert result == [dict(t) for t in terms]

    def test_input_dicts_not_mutated(self):
        """Input dicts must not be modified in-place."""
        terms = [{"term": "immutable", "frequency": 1, "category": "concept", "context_snippet": "ctx"}]
        original_copy = dict(terms[0])
        payload = {"definitions": [{"term": "immutable", "definition": "A def."}]}
        client = _client_returning(payload)
        refine_vocabulary_definitions(terms, client=client)
        assert terms[0] == original_copy


# ---------------------------------------------------------------------------
# refine_project_narratives
# ---------------------------------------------------------------------------


class TestRefineProjectNarratives:
    def test_empty_input_returns_empty(self):
        client = _client_returning({"narratives": []})
        result = refine_project_narratives([], client=client)
        assert result == []

    def test_unavailable_client_returns_input_unchanged(self):
        projects = _sample_projects(2)
        result = refine_project_narratives(projects, client=_unavailable_client())
        assert result == [dict(p) for p in projects]

    def test_none_client_falls_back_to_unavailable(self):
        projects = _sample_projects(2)
        with patch(
            "extractors.llm.refine_ontology._get_client", return_value=None
        ):
            result = refine_project_narratives(projects, client=None)
        assert result == [dict(p) for p in projects]

    def test_sensible_response_merged_correctly(self):
        projects = _sample_projects(2)
        payload = {
            "narratives": [
                {"cwd": "proj0", "narrative": "Project 0 does thing 0."},
                {"cwd": "proj1", "narrative": "Project 1 is for task 1."},
            ]
        }
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)

        assert len(result) == 2
        assert result[0]["cwd"] == "proj0"
        assert result[0]["narrative"] == "Project 0 does thing 0."
        assert result[1]["narrative"] == "Project 1 is for task 1."

    def test_unknown_cwd_from_llm_dropped(self):
        projects = [{"cwd": "real-proj", "display_name": "Real", "context_snippets": ["text"]}]
        payload = {
            "narratives": [
                {"cwd": "real-proj", "narrative": "The real one."},
                {"cwd": "fake-proj", "narrative": "Should be dropped."},
            ]
        }
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)

        assert len(result) == 1
        assert result[0]["cwd"] == "real-proj"
        assert result[0]["narrative"] == "The real one."
        assert not any(p.get("cwd") == "fake-proj" for p in result)

    def test_overlong_narrative_rejected(self):
        projects = [{"cwd": "p", "display_name": "P", "context_snippets": ["x"]}]
        long_text = "B" * 301
        payload = {"narratives": [{"cwd": "p", "narrative": long_text}]}
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)
        assert result[0].get("narrative") is None

    def test_llm_returns_none_payload_input_unchanged(self):
        projects = _sample_projects(2)
        client = _client_returning(None)
        result = refine_project_narratives(projects, client=client)
        assert result == [dict(p) for p in projects]

    def test_null_narrative_propagated(self):
        """LLM returning null means low confidence — propagate None."""
        projects = [{"cwd": "sparse", "display_name": "Sparse", "context_snippets": []}]
        payload = {"narratives": [{"cwd": "sparse", "narrative": None}]}
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)
        assert result[0].get("narrative") is None

    def test_no_new_projects_added(self):
        projects = _sample_projects(2)
        payload = {
            "narratives": [
                {"cwd": "proj0", "narrative": "N0"},
                {"cwd": "proj1", "narrative": "N1"},
                {"cwd": "phantom-proj", "narrative": "Should not appear"},
            ]
        }
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)
        assert len(result) == len(projects)

    def test_batching_large_input(self):
        projects = _sample_projects(30)
        call_count = 0

        def side_effect(system, user, schema=None):
            nonlocal call_count
            call_count += 1
            return {"narratives": []}

        client = MagicMock()
        client.available = True
        client.generate_json.side_effect = side_effect

        refine_project_narratives(projects, client=client)
        assert call_count == 2  # ceil(30 / 25)

    def test_generate_json_exception_returns_input_unchanged(self):
        projects = _sample_projects(2)
        client = MagicMock()
        client.available = True
        client.generate_json.side_effect = RuntimeError("timeout")
        result = refine_project_narratives(projects, client=client)
        assert result == [dict(p) for p in projects]

    def test_input_order_preserved(self):
        projects = [
            {"cwd": "z-proj", "display_name": "Z", "context_snippets": ["z"]},
            {"cwd": "a-proj", "display_name": "A", "context_snippets": ["a"]},
        ]
        payload = {
            "narratives": [
                {"cwd": "a-proj", "narrative": "A narrative."},
                {"cwd": "z-proj", "narrative": "Z narrative."},
            ]
        }
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)
        assert result[0]["cwd"] == "z-proj"
        assert result[0]["narrative"] == "Z narrative."
        assert result[1]["cwd"] == "a-proj"
        assert result[1]["narrative"] == "A narrative."

    def test_input_dicts_not_mutated(self):
        projects = [{"cwd": "immutable-proj", "display_name": "I", "context_snippets": ["ctx"]}]
        original_copy = dict(projects[0])
        payload = {"narratives": [{"cwd": "immutable-proj", "narrative": "A narrative."}]}
        client = _client_returning(payload)
        refine_project_narratives(projects, client=client)
        assert projects[0] == original_copy


# ---------------------------------------------------------------------------
# _is_echo unit tests
# ---------------------------------------------------------------------------


class TestIsEcho:
    def test_verbatim_substring_output_in_source(self):
        source = "The operator uses sharechain to link p2pool shares together."
        output = "uses sharechain to link p2pool shares"
        assert _is_echo(output, source) is True

    def test_verbatim_substring_source_in_output(self):
        source = "short snippet"
        output = "This is a short snippet that has been slightly extended."
        assert _is_echo(output, source) is True

    def test_high_token_overlap_is_echo(self):
        source = "the operator uses sharechain to link p2pool shares for payouts"
        # Definition is a near-copy with minimal rearrangement — high overlap
        output = "the operator uses sharechain link p2pool shares payouts"
        assert _is_echo(output, source) is True

    def test_synthesised_output_not_echo(self):
        source = "fan-out parallel agents research wave then build wave non-overlapping files"
        output = "A workflow pattern where multiple agents work in parallel on separate subtasks."
        assert _is_echo(output, source) is False

    def test_empty_output_not_echo(self):
        assert _is_echo("", "some source text") is False

    def test_empty_source_not_echo(self):
        assert _is_echo("some output text", "") is False

    def test_threshold_boundary(self):
        # Construct tokens so overlap is exactly at threshold.
        # output has 5 tokens, 3 shared with source → overlap = 3/5 = 0.6 (== threshold)
        output = "alpha beta gamma delta epsilon"
        source = "alpha beta gamma other stuff here"
        assert _is_echo(output, source, threshold=0.6) is True
        # One fewer shared token → 2/5 = 0.4 < 0.6
        output2 = "alpha beta zeta delta epsilon"
        assert _is_echo(output2, source, threshold=0.6) is False


# ---------------------------------------------------------------------------
# Anti-echo integration: refine_vocabulary_definitions
# ---------------------------------------------------------------------------


class TestAntiEchoVocabulary:
    def test_echo_definition_suppressed(self):
        """Model returns the input snippet as the definition — must be nulled."""
        snippet = "The operator uses sharechain to link p2pool shares together for payouts."
        terms = [
            {
                "term": "sharechain",
                "frequency": 10,
                "category": "concept",
                "context_snippet": snippet,
            }
        ]
        # Model parrots the snippet almost verbatim — high token overlap
        echo_definition = "The operator uses sharechain to link p2pool shares together for payouts."
        payload = {"definitions": [{"term": "sharechain", "definition": echo_definition}]}
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)

        assert len(result) == 1
        assert result[0]["definition"] is None, (
            "Echo definition should be suppressed to None"
        )

    def test_synthesised_definition_kept(self):
        """Model returns a genuinely different sentence — must be kept."""
        snippet = "fan-out parallel agents research wave then build wave non-overlapping files"
        terms = [
            {
                "term": "fan-out",
                "frequency": 5,
                "category": "workflow",
                "context_snippet": snippet,
            }
        ]
        # Genuinely synthesised — different vocabulary, low token overlap
        synthesised = "A workflow pattern where multiple agents work in parallel on separate subtasks."
        payload = {"definitions": [{"term": "fan-out", "definition": synthesised}]}
        client = _client_returning(payload)
        result = refine_vocabulary_definitions(terms, client=client)

        assert len(result) == 1
        assert result[0]["definition"] == synthesised, (
            "Synthesised definition should be kept"
        )


# ---------------------------------------------------------------------------
# Anti-echo integration: refine_project_narratives
# ---------------------------------------------------------------------------


class TestAntiEchoNarratives:
    def test_echo_narrative_suppressed(self):
        """Model returns the input snippets as the narrative — must be nulled."""
        snippets = [
            "returns the real client IP even behind NAT",
            "tiny Go HTTP service used by sidecar relay fleet",
        ]
        projects = [
            {
                "cwd": "ip-service-for-docker",
                "display_name": "IP Service",
                "context_snippets": snippets,
            }
        ]
        # Model echoes the joined snippets almost verbatim
        echo_narrative = "returns the real client IP even behind NAT tiny Go HTTP service used by sidecar relay fleet"
        payload = {"narratives": [{"cwd": "ip-service-for-docker", "narrative": echo_narrative}]}
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)

        assert len(result) == 1
        assert result[0]["narrative"] is None, (
            "Echo narrative should be suppressed to None"
        )

    def test_synthesised_narrative_kept(self):
        """Model returns a genuine 1-2 sentence summary — must be kept."""
        snippets = [
            "returns the real client IP even behind NAT",
            "tiny Go HTTP service used by sidecar relay fleet",
        ]
        projects = [
            {
                "cwd": "ip-service-for-docker",
                "display_name": "IP Service",
                "context_snippets": snippets,
            }
        ]
        synthesised = (
            "A lightweight Go HTTP service that reports the real client IP address, "
            "used by the sidecar relay infrastructure."
        )
        payload = {"narratives": [{"cwd": "ip-service-for-docker", "narrative": synthesised}]}
        client = _client_returning(payload)
        result = refine_project_narratives(projects, client=client)

        assert len(result) == 1
        assert result[0]["narrative"] == synthesised, (
            "Synthesised narrative should be kept"
        )
