"""Unit tests for src/api_clients — OpenAICompatibleClient and provider selection.

Uses httpx.MockTransport (no real LLM server). Covers: strict-JSON chat via
``response_format=json_schema``, the auth header, truncation and empty-answer handling,
availability probe, context-manager close, ``__repr__``, and ``create_llm`` dispatch.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.api_clients import (
    LlmError,
    OllamaClient,
    OllamaError,
    OpenAICompatibleClient,
    create_llm,
)

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}}


def _client_with(handler, **kwargs) -> OpenAICompatibleClient:
    oc = OpenAICompatibleClient(**kwargs)
    oc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return oc


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/chat/completions"):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"answer": 42}'},
                    }
                ]
            },
        )
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": []})
    return httpx.Response(404)


class TestChat:
    def test_chat_parses_json_content(self):
        oc = _client_with(_ok_handler)
        assert oc.chat("sys", "user", SCHEMA) == {"answer": 42}
        oc.close()

    def test_request_constrains_decoding_to_the_given_schema(self):
        """The schema the stage passed must reach the server as a json_schema format."""
        seen: dict = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return _ok_handler(request)

        oc = _client_with(handler, model="test-model")
        oc.chat("sys", "user", SCHEMA)
        oc.close()

        assert seen["model"] == "test-model"
        assert seen["temperature"] == 0
        assert seen["response_format"]["type"] == "json_schema"
        assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
        assert seen["response_format"]["json_schema"]["strict"] is True
        assert [m["role"] for m in seen["messages"]] == ["system", "user"]

    def test_per_call_model_overrides_the_configured_one(self):
        seen: dict = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return _ok_handler(request)

        oc = _client_with(handler, model="configured")
        oc.chat("sys", "user", SCHEMA, model="per-call")
        oc.close()
        assert seen["model"] == "per-call"

    def test_truncated_answer_raises_instead_of_silently_losing_entries(self):
        """finish_reason=length must fail loudly.

        Constrained decoding keeps the prefix valid JSON, so a cut-off answer can parse
        cleanly while missing nodes the caller would read as "nothing found here".
        """

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"answer": 4'},
                        }
                    ]
                },
            )

        oc = _client_with(handler)
        with pytest.raises(LlmError, match="обрезан"):
            oc.chat("sys", "user", SCHEMA)
        oc.close()

    def test_empty_content_raises(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
            )

        oc = _client_with(handler)
        with pytest.raises(LlmError, match="Пустой ответ"):
            oc.chat("sys", "user", SCHEMA)
        oc.close()

    def test_reasoning_only_answer_names_that_cause(self):
        """A reasoning model can burn the budget on the trace and return no content."""

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": "",
                                "reasoning_content": "долго думал",
                            },
                        }
                    ]
                },
            )

        oc = _client_with(handler)
        with pytest.raises(LlmError, match="reasoning"):
            oc.chat("sys", "user", SCHEMA)
        oc.close()

    def test_non_json_content_raises(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "not json"}}
                    ]
                },
            )

        oc = _client_with(handler)
        with pytest.raises(LlmError, match="невалидный JSON"):
            oc.chat("sys", "user", SCHEMA)
        oc.close()

    def test_http_error_propagates(self):
        oc = _client_with(lambda request: httpx.Response(500, text="boom"))
        with pytest.raises(httpx.HTTPStatusError):
            oc.chat("sys", "user", SCHEMA)
        oc.close()


class TestAuthHeader:
    def test_api_key_is_sent_as_bearer(self):
        seen: dict = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return _ok_handler(request)

        oc = _client_with(handler, api_key="secret")
        oc.chat("sys", "user", SCHEMA)
        oc.close()
        assert seen["auth"] == "Bearer secret"

    def test_no_header_without_a_key(self):
        seen: dict = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return _ok_handler(request)

        oc = _client_with(handler, api_key="")
        oc.chat("sys", "user", SCHEMA)
        oc.close()
        assert seen["auth"] is None


class TestAvailability:
    def test_available_when_models_answers(self):
        oc = _client_with(_ok_handler)
        assert oc.available() is True
        oc.close()

    def test_unavailable_is_reported_not_raised(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        oc = _client_with(handler)
        assert oc.available() is False
        oc.close()


class TestLifecycle:
    def test_context_manager_closes(self):
        with _client_with(_ok_handler) as oc:
            assert oc.chat("sys", "user", SCHEMA) == {"answer": 42}
        assert oc._client.is_closed

    def test_repr_shows_base_and_model(self):
        r = repr(OpenAICompatibleClient(base="http://x/v1", model="m"))
        assert r.startswith("OpenAICompatibleClient(")
        assert "base=http://x/v1" in r and "model=m" in r

    def test_base_url_trailing_slash_is_normalized(self):
        assert OpenAICompatibleClient(base="http://x/v1/").base == "http://x/v1"


class TestProviderSelection:
    def test_default_provider_is_ollama(self, monkeypatch):
        """Historical behaviour stays the default: DVD_OLLAMA_* alone keeps working."""
        from src.common.config import settings

        monkeypatch.setattr(settings, "llm_provider", "ollama")
        client = create_llm()
        assert isinstance(client, OllamaClient)
        client.close()

    def test_openai_provider_returns_openai_client(self, monkeypatch):
        from src.common.config import settings

        monkeypatch.setattr(settings, "llm_provider", "openai")
        client = create_llm()
        assert isinstance(client, OpenAICompatibleClient)
        client.close()


class TestErrorHierarchy:
    def test_ollama_error_is_an_llm_error(self):
        """Callers can catch the provider-agnostic base and cover both backends."""
        assert issubclass(OllamaError, LlmError)
