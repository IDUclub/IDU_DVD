"""Unit tests for src/api_clients — GigaEmbeddingsClient and create_embedder.

Uses httpx.MockTransport (no real vectorizer). Covers: OpenAI-schema parsing (index order),
document/query prompt routing, availability probe, error handling for missing data,
provider selection, context-manager close, and __repr__.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.api_clients import (
    EmbeddingsError,
    GigaEmbeddingsClient,
    OllamaClient,
    create_embedder,
)


def _client_with(handler) -> GigaEmbeddingsClient:
    ec = GigaEmbeddingsClient()
    ec._client = httpx.Client(transport=httpx.MockTransport(handler))
    return ec


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/embeddings":
        texts = json.loads(request.content)["input"]
        # deliberately out of order — the client must sort by index
        data = [
            {"object": "embedding", "embedding": [float(i)], "index": i}
            for i in reversed(range(len(texts)))
        ]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": "ai-sage/Giga-Embeddings-instruct",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )
    if path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(404)


class TestEmbed:
    def test_embed_returns_vectors_sorted_by_index(self):
        ec = _client_with(_ok_handler)
        assert ec.embed(["a", "b", "c"]) == [[0.0], [1.0], [2.0]]
        ec.close()

    def test_missing_data_raises(self):
        def handler(request):
            return httpx.Response(200, json={"object": "list", "data": []})

        ec = _client_with(handler)
        with pytest.raises(EmbeddingsError):
            ec.embed(["t"])
        ec.close()


class TestPromptRouting:
    def _capture(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _ok_handler(request)

        return seen, _client_with(handler)

    def test_documents_send_empty_prompt(self):
        seen, ec = self._capture()
        ec.embed_documents(["документ"])
        assert seen[0]["prompt"] == ""
        ec.close()

    def test_query_sends_instruction_prompt(self):
        seen, ec = self._capture()
        vector = ec.embed_query("вопрос")
        assert seen[0]["prompt"] == ec.query_prompt and ec.query_prompt
        assert vector == [0.0]  # single vector, not a batch
        ec.close()

    def test_none_prompt_is_omitted(self):
        seen, ec = self._capture()
        ec.embed(["t"])  # no prompt — defer to the service default
        assert "prompt" not in seen[0]
        ec.close()


class TestAvailability:
    def test_available_true_on_200(self):
        ec = _client_with(_ok_handler)
        assert ec.available() is True
        ec.close()

    def test_available_false_on_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        ec = _client_with(handler)
        assert ec.available() is False
        ec.close()


class TestProviderSelection:
    def test_default_provider_is_giga(self, monkeypatch):
        from src.common.config import settings

        monkeypatch.setattr(settings, "embeddings_provider", "giga")
        client = create_embedder()
        assert isinstance(client, GigaEmbeddingsClient)
        client.close()

    def test_ollama_provider_returns_ollama_client(self, monkeypatch):
        from src.common.config import settings

        monkeypatch.setattr(settings, "embeddings_provider", "ollama")
        client = create_embedder()
        assert isinstance(client, OllamaClient)
        client.close()


class TestLifecycleAndRepr:
    def test_context_manager_closes_client(self):
        ec = _client_with(_ok_handler)
        with ec as c:
            assert c is ec
        assert ec._client.is_closed

    def test_repr_mentions_base_and_model(self):
        r = repr(GigaEmbeddingsClient())
        assert r.startswith("GigaEmbeddingsClient(") and "base=" in r and "model=" in r
