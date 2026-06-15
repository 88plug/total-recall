"""Tests for extractors.llm.client and extractors.llm.cache.

All tests are hermetic — no real network calls, no real ollama daemon needed.
urllib.request.urlopen is monkeypatched via pytest's monkeypatch fixture.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from extractors.llm import LLMCache, LLMClient, get_default_client
from extractors.llm.client import DEFAULT_BASE_URL, DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_urlopen_response(body: bytes, status: int = 200) -> MagicMock:
    """Return a context-manager mock that mimics urllib urlopen's return value."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.status = status
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _tags_response(model_names: list[str]) -> bytes:
    """Build a fake /api/tags JSON body."""
    models = [{"name": n} for n in model_names]
    return json.dumps({"models": models}).encode()


def _generate_response(content: dict[str, Any]) -> bytes:
    """Build a fake /api/generate JSON body."""
    return json.dumps({"response": json.dumps(content), "done": True}).encode()


# ---------------------------------------------------------------------------
# LLMClient.available — probe scenarios
# ---------------------------------------------------------------------------


class TestLLMClientAvailable:
    def test_available_when_model_pulled(self):
        """ollama present and target model in list → available True."""
        tags_body = _tags_response([DEFAULT_MODEL])

        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(tags_body)):
            client = LLMClient(provider="auto", model=DEFAULT_MODEL)
            assert client.available is True

    def test_available_model_with_latest_suffix(self):
        """ollama returns 'model:latest' but we configured 'model' → still available."""
        model_bare = "mymodel"
        tags_body = _tags_response([f"{model_bare}:latest"])

        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(tags_body)):
            client = LLMClient(provider="auto", model=model_bare)
            assert client.available is True

    def test_unavailable_when_ollama_unreachable(self):
        """Daemon not running → URLError → available False, no exception raised."""
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            client = LLMClient(provider="auto")
            assert client.available is False

    def test_unavailable_when_model_not_pulled(self):
        """Daemon running but model missing from list → available False."""
        tags_body = _tags_response(["other-model:latest"])

        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(tags_body)):
            client = LLMClient(provider="auto", model="not-pulled-model")
            assert client.available is False

    def test_unavailable_when_provider_none(self):
        """provider='none' → always False without any network call."""
        with patch("urllib.request.urlopen") as mock_open:
            client = LLMClient(provider="none")
            assert client.available is False
            mock_open.assert_not_called()

    def test_probe_cached_after_first_call(self):
        """Second access to .available does not re-probe the network."""
        tags_body = _tags_response([DEFAULT_MODEL])
        mock_resp = _make_urlopen_response(tags_body)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            client = LLMClient(provider="auto", model=DEFAULT_MODEL)
            _ = client.available
            _ = client.available  # second access
            assert mock_open.call_count == 1


# ---------------------------------------------------------------------------
# LLMClient.generate_json
# ---------------------------------------------------------------------------


class TestGenerateJson:
    def _client_available(self, model: str = DEFAULT_MODEL) -> LLMClient:
        """Return a client whose _available is pre-set to True (skip probe)."""
        client = LLMClient(provider="auto", model=model)
        client._available = True  # noqa: SLF001
        return client

    def test_returns_parsed_dict_on_success(self):
        """Successful generate call returns the parsed response dict."""
        expected = {"machines": ["server1", "server2"]}
        gen_body = _generate_response(expected)

        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(gen_body)):
            client = self._client_available()
            result = client.generate_json(system="sys", user="user prompt")

        assert result == expected

    def test_returns_none_when_unavailable(self):
        """generate_json returns None immediately when not available."""
        client = LLMClient(provider="none")
        # available is False without network call
        with patch("urllib.request.urlopen") as mock_open:
            result = client.generate_json(system="sys", user="user prompt")
            mock_open.assert_not_called()
        assert result is None

    def test_returns_none_on_network_error(self):
        """URLError during generate → None, no exception propagated."""
        client = self._client_available()
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            result = client.generate_json(system="sys", user="content")
        assert result is None

    def test_returns_none_on_invalid_outer_json(self):
        """Daemon returns non-JSON body → None."""
        client = self._client_available()
        with patch(
            "urllib.request.urlopen",
            return_value=_make_urlopen_response(b"not json at all"),
        ):
            result = client.generate_json(system="sys", user="content")
        assert result is None

    def test_returns_none_on_invalid_inner_json(self):
        """Model response field contains invalid JSON → None."""
        client = self._client_available()
        bad_body = json.dumps({"response": "this is not json {", "done": True}).encode()
        with patch(
            "urllib.request.urlopen",
            return_value=_make_urlopen_response(bad_body),
        ):
            result = client.generate_json(system="sys", user="content")
        assert result is None

    def test_passes_schema_as_format_when_provided(self):
        """When schema is given it is sent as the format field."""
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        expected = {"name": "hal"}
        gen_body = _generate_response(expected)

        captured: dict[str, Any] = {}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            captured["body"] = json.loads(req.data)
            return _make_urlopen_response(gen_body)

        client = self._client_available()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.generate_json(system="sys", user="u", schema=schema)

        assert result == expected
        assert captured["body"]["format"] == schema

    def test_uses_json_format_without_schema(self):
        """Without schema, format is the string 'json'."""
        expected = {"key": "val"}
        gen_body = _generate_response(expected)

        captured: dict[str, Any] = {}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            captured["body"] = json.loads(req.data)
            return _make_urlopen_response(gen_body)

        client = self._client_available()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.generate_json(system="sys", user="u")

        assert captured["body"]["format"] == "json"


# ---------------------------------------------------------------------------
# LLMClient cache integration
# ---------------------------------------------------------------------------


class TestGenerateJsonCache:
    def _client_available_with_cache(self, tmp_path: Path, model: str = DEFAULT_MODEL) -> LLMClient:
        """Return an available client backed by a fresh tmp LLMCache."""
        from extractors.llm.cache import LLMCache as _LLMCache
        cache = _LLMCache(tmp_path / "llm_cache.db")
        client = LLMClient(provider="auto", model=model, cache=cache)
        client._available = True  # noqa: SLF001
        return client

    def test_cache_hit_skips_http(self, tmp_path: Path):
        """Second call with identical args is served from cache; urlopen called once."""
        expected = {"entities": ["host1", "host2"]}
        gen_body = _generate_response(expected)

        client = self._client_available_with_cache(tmp_path)
        with patch(
            "urllib.request.urlopen", return_value=_make_urlopen_response(gen_body)
        ) as mock_open:
            result1 = client.generate_json(system="sys", user="tell me about machines")
            result2 = client.generate_json(system="sys", user="tell me about machines")

        assert mock_open.call_count == 1
        assert result1 == expected
        assert result2 == expected

    def test_cache_miss_then_put(self, tmp_path: Path):
        """First call misses the cache (hits network) and stores the result;
        second call is a hit."""
        from extractors.llm.cache import LLMCache as _LLMCache

        expected = {"vocab": ["term1"]}
        gen_body = _generate_response(expected)
        db = tmp_path / "llm_cache.db"

        client = LLMClient(provider="auto", model=DEFAULT_MODEL, cache=_LLMCache(db))
        client._available = True  # noqa: SLF001

        with patch(
            "urllib.request.urlopen", return_value=_make_urlopen_response(gen_body)
        ) as mock_open:
            result1 = client.generate_json(system="s", user="u")

        assert mock_open.call_count == 1
        assert result1 == expected

        # Verify the cache was populated by reading it directly.
        cache_direct = _LLMCache(db)
        cached = cache_direct.get(model=DEFAULT_MODEL, system="s", user="u", schema_json=None)
        assert cached == expected

        # Second call from the same client — no new network call.
        with patch(
            "urllib.request.urlopen", return_value=_make_urlopen_response(gen_body)
        ) as mock_open2:
            result2 = client.generate_json(system="s", user="u")

        assert mock_open2.call_count == 0
        assert result2 == expected

    def test_cache_none_does_not_break_generate(self, tmp_path: Path):
        """Client with cache=None works exactly as before (no AttributeError)."""
        expected = {"key": "val"}
        gen_body = _generate_response(expected)

        client = LLMClient(provider="auto", model=DEFAULT_MODEL, cache=None)
        client._available = True  # noqa: SLF001

        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(gen_body)):
            result = client.generate_json(system="sys", user="u")

        assert result == expected

    def test_different_args_are_separate_cache_entries(self, tmp_path: Path):
        """Two calls with different user prompts each hit the network."""
        expected_a = {"x": 1}
        expected_b = {"x": 2}

        call_count = 0

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            body = _generate_response(expected_a if call_count == 1 else expected_b)
            return _make_urlopen_response(body)

        client = self._client_available_with_cache(tmp_path)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            r1 = client.generate_json(system="sys", user="prompt-A")
            r2 = client.generate_json(system="sys", user="prompt-B")

        assert call_count == 2
        assert r1 == expected_a
        assert r2 == expected_b


# ---------------------------------------------------------------------------
# LLMCache
# ---------------------------------------------------------------------------


class TestLLMCache:
    def test_cache_miss_returns_none(self, tmp_path: Path):
        cache = LLMCache(tmp_path / "llm_cache.db")
        result = cache.get(model="m", system="s", user="u")
        assert result is None

    def test_put_then_get_round_trip(self, tmp_path: Path):
        cache = LLMCache(tmp_path / "llm_cache.db")
        data = {"entities": ["machineA", "machineB"], "count": 2}
        cache.put(model="gemma4:e2b", system="sys", user="u", schema_json=None, result=data)
        retrieved = cache.get(model="gemma4:e2b", system="sys", user="u", schema_json=None)
        assert retrieved == data

    def test_schema_json_included_in_key(self, tmp_path: Path):
        """Different schema → different cache key → miss for the other schema."""
        cache = LLMCache(tmp_path / "llm_cache.db")
        data = {"x": 1}
        schema_a = '{"type": "object"}'
        schema_b = '{"type": "array"}'

        cache.put(model="m", system="s", user="u", schema_json=schema_a, result=data)

        assert cache.get(model="m", system="s", user="u", schema_json=schema_a) == data
        assert cache.get(model="m", system="s", user="u", schema_json=schema_b) is None

    def test_model_included_in_key(self, tmp_path: Path):
        """Different model → different cache key."""
        cache = LLMCache(tmp_path / "llm_cache.db")
        data = {"v": 42}
        cache.put(model="model-a", system="s", user="u", schema_json=None, result=data)
        assert cache.get(model="model-b", system="s", user="u") is None
        assert cache.get(model="model-a", system="s", user="u") == data

    def test_overwrite_with_insert_or_replace(self, tmp_path: Path):
        """Second put for the same key overwrites the first."""
        cache = LLMCache(tmp_path / "llm_cache.db")
        cache.put(model="m", system="s", user="u", schema_json=None, result={"v": 1})
        cache.put(model="m", system="s", user="u", schema_json=None, result={"v": 2})
        assert cache.get(model="m", system="s", user="u") == {"v": 2}

    def test_persistence_across_instances(self, tmp_path: Path):
        """Data written by one instance is readable by a new instance on the same DB."""
        db = tmp_path / "llm_cache.db"
        data = {"hello": "world"}
        LLMCache(db).put(model="m", system="s", user="u", schema_json=None, result=data)
        assert LLMCache(db).get(model="m", system="s", user="u") == data


# ---------------------------------------------------------------------------
# get_default_client — env-var parsing
# ---------------------------------------------------------------------------


class TestGetDefaultClient:
    def test_defaults_when_no_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TOTAL_RECALL_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("TOTAL_RECALL_LLM_MODEL", raising=False)
        monkeypatch.delenv("TOTAL_RECALL_LLM_BASE_URL", raising=False)

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no daemon")):
            client = get_default_client()

        assert client.provider == "auto"
        # autoselect_model() always returns DEFAULT_MODEL ("qwen3.5:2b").
        assert client.model == DEFAULT_MODEL
        assert client.base_url == DEFAULT_BASE_URL

    def test_env_provider_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "none")
        monkeypatch.delenv("TOTAL_RECALL_LLM_MODEL", raising=False)
        monkeypatch.delenv("TOTAL_RECALL_LLM_BASE_URL", raising=False)

        client = get_default_client()
        assert client.provider == "none"
        assert client.available is False

    def test_env_custom_model_and_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("TOTAL_RECALL_LLM_MODEL", "llama3:8b")
        monkeypatch.setenv("TOTAL_RECALL_LLM_BASE_URL", "http://192.168.1.10:11434")

        tags_body = _tags_response(["llama3:8b"])
        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(tags_body)):
            client = get_default_client()
            # Probe inside the patch context so mock is active when .available fires.
            available = client.available

        assert client.provider == "ollama"
        assert client.model == "llama3:8b"
        assert client.base_url == "http://192.168.1.10:11434"
        assert available is True

    def test_env_provider_auto_with_running_daemon(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TOTAL_RECALL_LLM_PROVIDER", "auto")
        monkeypatch.setenv("TOTAL_RECALL_LLM_MODEL", DEFAULT_MODEL)
        monkeypatch.delenv("TOTAL_RECALL_LLM_BASE_URL", raising=False)

        tags_body = _tags_response([DEFAULT_MODEL])
        with patch("urllib.request.urlopen", return_value=_make_urlopen_response(tags_body)):
            client = get_default_client()
            available = client.available

        assert available is True
