"""Tests for extractors.llm.refine_machines.

All tests are hermetic — no real ollama / network calls.
The LLM client module may not exist yet (L1 agent builds it in parallel);
pytest.importorskip ensures the test file is skipped cleanly in that case.
"""

import pytest

# Skip the entire module cleanly if the L1 client hasn't landed yet.
pytest.importorskip("extractors.llm.client")

from unittest.mock import MagicMock, patch

from extractors.llm.refine_machines import refine_machines

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SCHEMA_FIELDS = {"role", "ip", "tailscale", "hits"}


def _make_entry(**kw) -> dict:
    base = {"role": None, "ip": None, "tailscale": False, "hits": 1}
    base.update(kw)
    return base


def _make_machines(*hostnames, **overrides) -> dict[str, dict]:
    return {h: _make_entry(**overrides) for h in hostnames}


def _unavailable_client() -> MagicMock:
    c = MagicMock()
    c.available = False
    return c


def _available_client(generate_json_return) -> MagicMock:
    c = MagicMock()
    c.available = True
    c.generate_json.return_value = generate_json_return
    return c


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestUnavailableClient:
    """When client.available is False the input is returned unchanged."""

    def test_returns_input_unchanged(self):
        machines = _make_machines("host-alpha", "relay-eu-1", "brute-force")
        client = _unavailable_client()

        result = refine_machines(machines, client=client)

        assert result is machines  # identical object, not a copy
        client.generate_json.assert_not_called()

    def test_default_client_unavailable(self):
        """Uses get_default_client() when client=None; if unavailable, passthrough."""
        machines = _make_machines("server-1", "hardening")
        fake_client = _unavailable_client()

        with patch(
            "extractors.llm.client.get_default_client",
            return_value=fake_client,
        ):
            result = refine_machines(machines, client=fake_client)

        assert result is machines


class TestSuccessfulFiltering:
    """Normal operation: LLM returns a keep list, schema is preserved."""

    def test_keeps_only_llm_approved_entries(self):
        machines = _make_machines(
            "host-alpha",
            "relay-eu-1",
            "brute-force",
            "hardening",
        )
        # Set distinct hit counts so we can verify the returned values.
        machines["host-alpha"]["hits"] = 42
        machines["relay-eu-1"]["hits"] = 17

        client = _available_client({"keep": ["host-alpha", "relay-eu-1"]})

        result = refine_machines(machines, client=client)

        assert set(result.keys()) == {"host-alpha", "relay-eu-1"}

    def test_original_schema_preserved(self):
        machines = {
            "host-alpha": {"role": "web", "ip": "10.0.0.1", "tailscale": True, "hits": 5},
            "relay-eu-1": {"role": "relay", "ip": "100.64.1.1", "tailscale": True, "hits": 3},
            "brute-force": {"role": None, "ip": None, "tailscale": False, "hits": 1},
        }
        client = _available_client({"keep": ["host-alpha", "relay-eu-1"]})

        result = refine_machines(machines, client=client)

        assert result["host-alpha"] == {
            "role": "web", "ip": "10.0.0.1", "tailscale": True, "hits": 5
        }
        assert result["relay-eu-1"] == {
            "role": "relay", "ip": "100.64.1.1", "tailscale": True, "hits": 3
        }
        assert "brute-force" not in result

    def test_all_schema_keys_present(self):
        machines = _make_machines("server-a", "noise-word")
        client = _available_client({"keep": ["server-a"]})

        result = refine_machines(machines, client=client)

        assert set(result["server-a"].keys()) == _SCHEMA_FIELDS


class TestHallucinationGuard:
    """LLM returns keys not present in the input — they must be dropped."""

    def test_hallucinated_entries_dropped(self):
        machines = _make_machines("host-alpha", "relay-eu-1", "brute-force", "hardening")
        # LLM invents "never-was-input" which is not a key in machines.
        client = _available_client({"keep": ["never-was-input", "host-alpha"]})

        result = refine_machines(machines, client=client)

        assert "never-was-input" not in result
        assert "host-alpha" in result

    def test_all_hallucinated_returns_empty(self):
        machines = _make_machines("real-host")
        client = _available_client({"keep": ["ghost-machine", "phantom-server"]})

        result = refine_machines(machines, client=client)

        assert result == {}


class TestLLMFailureFallback:
    """When generate_json returns None the input is returned unchanged."""

    def test_none_response_falls_back_to_input(self):
        machines = _make_machines("web-01", "db-02", "brute-force")
        client = _available_client(None)

        result = refine_machines(machines, client=client)

        # generate_json returning None means we fall back to keeping the
        # whole chunk — so result should equal input.
        assert set(result.keys()) == set(machines.keys())

    def test_exception_in_generate_json_falls_back(self):
        machines = _make_machines("server-a", "noise")
        client = MagicMock()
        client.available = True
        client.generate_json.side_effect = RuntimeError("connection refused")

        result = refine_machines(machines, client=client)

        assert result is machines


class TestEmptyInput:
    """Empty input dict is returned immediately without calling the LLM."""

    def test_empty_dict_no_llm_call(self):
        client = _available_client({"keep": []})

        result = refine_machines({}, client=client)

        assert result == {}
        client.generate_json.assert_not_called()


class TestBatching:
    """Inputs with >200 entries are batched in chunks of 100."""

    def test_large_input_batched(self):
        # 250 entries: 3 chunks (100, 100, 50).
        real_hosts = [f"server-{i:03d}" for i in range(150)]
        noise_hosts = [f"noisyword{i}" for i in range(100)]
        all_hosts = real_hosts + noise_hosts
        machines = _make_machines(*all_hosts)

        # The mock returns only entries that start with "server-".
        def smart_generate_json(system, user, schema=None, **kw):
            import re
            # Extract quoted tokens from the prompt.
            found = re.findall(r"'(server-\d+)'", user)
            return {"keep": found}

        client = MagicMock()
        client.available = True
        client.generate_json.side_effect = smart_generate_json

        result = refine_machines(machines, client=client)

        # All real hosts kept; noise dropped.
        assert set(result.keys()) == set(real_hosts)
        # Should have been called 3 times (ceil(250/100)).
        assert client.generate_json.call_count == 3

    def test_exactly_100_entries_single_call(self):
        """100 entries fit in one chunk (chunk size is 100, condition is <=)."""
        machines = _make_machines(*[f"host-{i}" for i in range(100)])
        client = _available_client({"keep": list(machines.keys())})

        result = refine_machines(machines, client=client)

        assert client.generate_json.call_count == 1
        assert set(result.keys()) == set(machines.keys())

    def test_101_entries_two_calls(self):
        """101 entries triggers batching (100 + 1 = 2 calls)."""
        machines = _make_machines(*[f"host-{i}" for i in range(101)])
        client = _available_client({"keep": []})

        refine_machines(machines, client=client)

        assert client.generate_json.call_count == 2  # 100 + 1


class TestOperatorEmailContext:
    """operator_email domain is passed to the LLM for disambiguation."""

    def test_email_domain_included_in_prompt(self):
        machines = _make_machines("mailhost", "cloudflare")
        client = _available_client({"keep": ["mailhost"]})

        refine_machines(machines, operator_email="admin@example.com", client=client)

        call_kwargs = client.generate_json.call_args
        # The user prompt should mention the domain.
        user_prompt = call_kwargs[1].get("user") or call_kwargs[0][1]
        assert "example.com" in user_prompt
        # Full email address should NOT appear in the prompt.
        assert "admin@example.com" not in user_prompt

    def test_none_email_no_crash(self):
        machines = _make_machines("relay-1", "noise")
        client = _available_client({"keep": ["relay-1"]})

        result = refine_machines(machines, operator_email=None, client=client)

        assert "relay-1" in result


class TestSampleContexts:
    """sample_contexts snippets are included in the user prompt."""

    def test_context_snippets_appear_in_prompt(self):
        machines = _make_machines("relay-eu-1")
        contexts = {"relay-eu-1": ["ssh user@relay-eu-1", "deploy to relay-eu-1"]}
        client = _available_client({"keep": ["relay-eu-1"]})

        refine_machines(machines, sample_contexts=contexts, client=client)

        call_kwargs = client.generate_json.call_args
        user_prompt = call_kwargs[1].get("user") or call_kwargs[0][1]
        assert "relay-eu-1" in user_prompt
        assert "ssh user@relay-eu-1" in user_prompt

    def test_none_contexts_no_crash(self):
        machines = _make_machines("web-01", "brute-force")
        client = _available_client({"keep": ["web-01"]})

        result = refine_machines(machines, sample_contexts=None, client=client)

        assert "web-01" in result


class TestNonStringInKeep:
    """Non-string elements in the model's 'keep' list must not crash or pollute."""

    def test_non_string_elements_filtered_out(self):
        machines = _make_machines("host-a", "relay-1")
        # Model returns a mix: one valid string, one int, one null.
        client = _available_client({"keep": ["host-a", 123, None]})

        result = refine_machines(machines, client=client)

        # Only the real string that is also in the input survives.
        assert set(result.keys()) == {"host-a"}

    def test_all_non_string_keep_returns_empty(self):
        machines = _make_machines("real-host")
        client = _available_client({"keep": [42, None, 3.14]})

        result = refine_machines(machines, client=client)

        assert result == {}

    def test_no_crash_mixed_types(self):
        """Must not raise even with wild types in the keep list."""
        machines = _make_machines("server-a", "server-b")
        client = _available_client({"keep": ["server-a", 0, False, None, [], {}]})

        result = refine_machines(machines, client=client)

        assert set(result.keys()) == {"server-a"}


class TestBareHostnameWithContext:
    """Bare single-word hostnames should be kept when context supports them."""

    def test_bare_hostname_kept_with_machine_context(self):
        machines = _make_machines("novabox")
        contexts = {"novabox": ["ssh dana@novabox", "novabox rebooted"]}
        # Model decides to keep it given the context.
        client = _available_client({"keep": ["novabox"]})

        result = refine_machines(machines, sample_contexts=contexts, client=client)

        assert "novabox" in result

    def test_bare_hostname_context_appears_in_prompt(self):
        machines = _make_machines("novabox")
        contexts = {"novabox": ["ssh dana@novabox", "novabox rebooted"]}
        client = _available_client({"keep": ["novabox"]})

        refine_machines(machines, sample_contexts=contexts, client=client)

        call_kwargs = client.generate_json.call_args
        user_prompt = call_kwargs[1].get("user") or call_kwargs[0][1]
        assert "ssh dana@novabox" in user_prompt
        assert "novabox rebooted" in user_prompt

    def test_bare_hostname_dropped_without_context(self):
        machines = _make_machines("novabox")
        # Model drops it when no context is present.
        client = _available_client({"keep": []})

        result = refine_machines(machines, sample_contexts=None, client=client)

        assert "novabox" not in result
        assert result == {}


class TestNoContextStillWorks:
    """sample_contexts=None path must work end-to-end without errors."""

    def test_none_sample_contexts_full_flow(self):
        machines = _make_machines("relay-eu-1", "brute-force", "hardening")
        client = _available_client({"keep": ["relay-eu-1"]})

        result = refine_machines(machines, sample_contexts=None, client=client)

        assert set(result.keys()) == {"relay-eu-1"}
        client.generate_json.assert_called_once()

    def test_none_sample_contexts_prompt_has_no_context_field(self):
        """When sample_contexts is None no '[context:' line appears in prompt."""
        machines = _make_machines("server-a")
        client = _available_client({"keep": ["server-a"]})

        refine_machines(machines, sample_contexts=None, client=client)

        call_kwargs = client.generate_json.call_args
        user_prompt = call_kwargs[1].get("user") or call_kwargs[0][1]
        assert "[context:" not in user_prompt
