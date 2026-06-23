"""Unit tests for src/api_clients — OllamaClient.

Uses httpx.MockTransport (no real Ollama). Covers: strict-JSON chat, embeddings, availability
probe, error handling for empty/missing payloads, context-manager close, and __repr__.
"""

from __future__ import annotations

import httpx
import pytest

from src.api_clients import OllamaClient, OllamaError


def _client_with(handler) -> OllamaClient:
    oc = OllamaClient()
    oc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return oc


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/chat":
        return httpx.Response(200, json={"message": {"content": '{"answer": 42}'}})
    if path == "/api/embed":
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
    if path == "/api/tags":
        return httpx.Response(200, json={"models": []})
    return httpx.Response(404)


class TestChat:
    def test_chat_parses_json_content(self):
        oc = _client_with(_ok_handler)
        assert oc.chat("sys", "user", {"type": "object"}) == {"answer": 42}
        oc.close()

    def test_empty_content_raises_ollama_error(self):
        def handler(request):
            return httpx.Response(200, json={"message": {"content": "   "}})

        oc = _client_with(handler)
        with pytest.raises(OllamaError):
            oc.chat("sys", "user", {})
        oc.close()


class TestEmbed:
    def test_embed_returns_vectors(self):
        oc = _client_with(_ok_handler)
        assert oc.embed(["t"]) == [[0.1, 0.2, 0.3]]
        oc.close()

    def test_missing_embeddings_raises(self):
        def handler(request):
            return httpx.Response(200, json={})

        oc = _client_with(handler)
        with pytest.raises(OllamaError):
            oc.embed(["t"])
        oc.close()


class TestAvailability:
    def test_available_true_on_200(self):
        oc = _client_with(_ok_handler)
        assert oc.available() is True
        oc.close()

    def test_available_false_on_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        oc = _client_with(handler)
        assert oc.available() is False
        oc.close()


class TestLifecycleAndRepr:
    def test_context_manager_closes_client(self):
        oc = _client_with(_ok_handler)
        with oc as c:
            assert c is oc
        assert oc._client.is_closed

    def test_repr_mentions_models(self):
        r = repr(OllamaClient())
        assert r.startswith("OllamaClient(") and "model=" in r and "embed_model=" in r
