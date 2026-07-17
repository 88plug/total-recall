"""Tests for extractors.llm.client.autoselect_model.

autoselect_model() always returns DEFAULT_MODEL ("qwen3.5:2b") — the outright
winner of a head-to-head CPU eval (coverage 0.60 / echo_rate 0.14 vs 4b same
coverage / worse echo, and 9b at 0.00 coverage due to think-mode leak).
No resource probes, no multi-tier registry.  The env override is the only way
to choose a different model.
"""

from __future__ import annotations

from extractors.llm.client import DEFAULT_MODEL, autoselect_model

# ---------------------------------------------------------------------------
# autoselect_model — always returns DEFAULT_MODEL
# ---------------------------------------------------------------------------


class TestAutoselectModel:
    def test_returns_default_model(self):
        """autoselect_model() returns DEFAULT_MODEL unconditionally."""
        assert autoselect_model() == DEFAULT_MODEL

    def test_returns_2b(self):
        """The returned value is the 2b tag — the eval winner."""
        assert autoselect_model() == "qwen3.5:2b"

    def test_ignores_env_override(self, monkeypatch):
        """autoselect_model() ignores TOTAL_RECALL_LLM_MODEL — the factory handles that."""
        monkeypatch.setenv("TOTAL_RECALL_LLM_MODEL", "some-other-model:13b")
        # autoselect_model itself is pure; the env read lives in get_default_client.
        assert autoselect_model() == DEFAULT_MODEL

    def test_never_raises(self):
        """autoselect_model() never raises even in weird environments."""
        result = autoselect_model()
        assert isinstance(result, str) and result


# ---------------------------------------------------------------------------
# get_default_client env override
# ---------------------------------------------------------------------------


class TestGetDefaultClientEnvOverride:
    def test_env_override_wins_over_autoselect(self, monkeypatch):
        """TOTAL_RECALL_LLM_MODEL env var bypasses autoselect entirely."""
        monkeypatch.setenv("TOTAL_RECALL_LLM_MODEL", "my-custom-model:latest")

        from extractors.llm.client import get_default_client

        client = get_default_client()
        assert client.model == "my-custom-model:latest"

    def test_no_env_uses_autoselect(self, monkeypatch):
        """Without env override, model comes from autoselect_model() → DEFAULT_MODEL."""
        monkeypatch.delenv("TOTAL_RECALL_LLM_MODEL", raising=False)

        from extractors.llm.client import get_default_client

        client = get_default_client()
        assert client.model == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# CLI smoke test — total-recall llm-model
# ---------------------------------------------------------------------------


class TestLlmModelCLI:
    def test_llm_model_cmd_prints_default_model(self, monkeypatch):
        """``total-recall llm-model`` prints DEFAULT_MODEL when no env override."""
        from click.testing import CliRunner

        from total_recall.__main__ import cli

        monkeypatch.delenv("TOTAL_RECALL_LLM_MODEL", raising=False)

        runner = CliRunner()
        result = runner.invoke(cli, ["llm-model"])

        assert result.exit_code == 0
        assert result.output.strip() == DEFAULT_MODEL

    def test_llm_model_cmd_respects_env_override(self, monkeypatch):
        """TOTAL_RECALL_LLM_MODEL env var is echoed directly by the CLI."""
        from click.testing import CliRunner

        from total_recall.__main__ import cli

        monkeypatch.setenv("TOTAL_RECALL_LLM_MODEL", "llama3:8b")

        runner = CliRunner(env={"TOTAL_RECALL_LLM_MODEL": "llama3:8b"})
        result = runner.invoke(cli, ["llm-model"])

        assert result.exit_code == 0
        assert result.output.strip() == "llama3:8b"
