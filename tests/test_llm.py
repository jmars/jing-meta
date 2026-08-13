"""Tests for dreamer.llm — cloud LLM client defaults and JSON extraction."""

from dreamer import llm
from jing_meta import config


def _fake_resp(content: str, prompt_tokens: int = 10, completion_tokens: int = 5):
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            }

    return _FakeResp()


class TestCallDefaults:
    """llm.call falls back to DeepInfra + Mistral-Small-24B when no env is set."""

    def test_uses_deepinfra_mistral_defaults(self, monkeypatch):
        # Point at the config defaults for URL/model, but a key is still required.
        for var in ("GRAPH_GARDENER_API_URL", "GRAPH_GARDENER_API_KEY", "GRAPH_GARDENER_MODEL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "test-key")

        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent["url"] = url
            sent["body"] = json
            sent["auth"] = headers.get("Authorization")
            return _fake_resp('{"add_relations": []}')

        monkeypatch.setattr(llm.requests, "post", fake_post)

        result, meta = llm.call("sys", "user")
        assert result == {"add_relations": []}
        assert meta["model"] == config.CLOUD_LLM_MODEL

        # The URL is the DeepInfra OpenAI endpoint + /chat/completions.
        assert sent["url"] == f"{config.CLOUD_LLM_URL}/chat/completions"
        assert sent["body"]["model"] == config.CLOUD_LLM_MODEL
        assert sent["auth"] == "Bearer test-key"

    def test_no_key_nonloopback_rejected(self, monkeypatch):
        # With no key and an explicit non-loopback host, call() bails before sending.
        monkeypatch.setenv("GRAPH_GARDENER_API_URL", "https://api.deepinfra.com/v1/openai")
        for var in ("GRAPH_GARDENER_API_KEY", "GRAPH_GARDENER_MODEL"):
            monkeypatch.delenv(var, raising=False)
        result, meta = llm.call("sys", "user")
        assert result is None and meta is None

    def test_env_overrides_defaults(self, monkeypatch):
        monkeypatch.setenv("GRAPH_GARDENER_API_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("GRAPH_GARDENER_MODEL", "deepseek-chat")
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")

        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent["url"] = url
            sent["body"] = json
            return _fake_resp('{"add_relations": []}')

        monkeypatch.setattr(llm.requests, "post", fake_post)
        result, meta = llm.call("sys", "user")
        assert meta["model"] == "deepseek-chat"
        assert sent["url"] == "https://api.deepseek.com/v1/chat/completions"


class TestServiceTier:
    """service_tier defaults to flex (the gardener's workload); env/param override."""

    def test_default_is_flex(self, monkeypatch):
        for var in ("GRAPH_GARDENER_API_URL", "GRAPH_GARDENER_API_KEY",
                    "GRAPH_GARDENER_MODEL", "JING_CLOUD_SERVICE_TIER"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")

        sent = {}
        def fake_post(url, headers=None, json=None, timeout=None):
            sent["body"] = json
            return _fake_resp('{"add_relations": []}')
        monkeypatch.setattr(llm.requests, "post", fake_post)

        llm.call("sys", "user")
        assert sent["body"].get("service_tier") == "flex"

    def test_env_standard_omits_service_tier(self, monkeypatch):
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")
        monkeypatch.setenv("JING_CLOUD_SERVICE_TIER", "")

        sent = {}
        def fake_post(url, headers=None, json=None, timeout=None):
            sent["body"] = json
            return _fake_resp('{"add_relations": []}')
        monkeypatch.setattr(llm.requests, "post", fake_post)

        llm.call("sys", "user")
        assert "service_tier" not in sent["body"]

    def test_env_priority_overrides(self, monkeypatch):
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")
        monkeypatch.setenv("JING_CLOUD_SERVICE_TIER", "priority")

        sent = {}
        def fake_post(url, headers=None, json=None, timeout=None):
            sent["body"] = json
            return _fake_resp('{"add_relations": []}')
        monkeypatch.setattr(llm.requests, "post", fake_post)

        llm.call("sys", "user")
        assert sent["body"].get("service_tier") == "priority"

    def test_param_overrides_env(self, monkeypatch):
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")
        monkeypatch.setenv("JING_CLOUD_SERVICE_TIER", "flex")

        sent = {}
        def fake_post(url, headers=None, json=None, timeout=None):
            sent["body"] = json
            return _fake_resp('{"add_relations": []}')
        monkeypatch.setattr(llm.requests, "post", fake_post)

        llm.call("sys", "user", service_tier="")
        assert "service_tier" not in sent["body"]


class TestHttpRetry:
    """Transient HTTP errors are retried with backoff, then fail open."""

    def test_http_error_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")
        calls = {"n": 0}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise llm.requests.ConnectionError("boom")
            return _fake_resp('{"add_relations": []}')

        monkeypatch.setattr(llm.requests, "post", fake_post)
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)

        result, meta = llm.call("sys", "user")
        assert result == {"add_relations": []}
        assert calls["n"] == 2

    def test_http_error_exhausts_retries_returns_none(self, monkeypatch):
        monkeypatch.setenv("GRAPH_GARDENER_API_KEY", "k")

        def fake_post(url, headers=None, json=None, timeout=None):
            raise llm.requests.ConnectionError("boom")

        monkeypatch.setattr(llm.requests, "post", fake_post)
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)

        result, meta = llm.call("sys", "user", http_retries=2)
        assert result is None and meta is None


class TestExtractJson:
    def test_code_fence_stripped(self):
        assert llm._extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}

    def test_prose_trailing_noise(self):
        assert llm._extract_json('Here you go: {"a": 1} trailing') == {"a": 1}

    def test_plain_object(self):
        assert llm._extract_json('{"a": 1}') == {"a": 1}
