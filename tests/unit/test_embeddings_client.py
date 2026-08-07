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


class TestRetryOn5xx:
    """Transient 5xx (e.g. a CUDA OOM on the shared a.dgx GPU) is retried with backoff."""

    @staticmethod
    def _capture_sleeps(monkeypatch) -> list[float]:
        sleeps: list[float] = []
        monkeypatch.setattr(
            "src.api_clients.embeddings_client.time.sleep",
            lambda seconds: sleeps.append(seconds),
        )
        return sleeps

    def test_succeeds_after_transient_500s(self, monkeypatch):
        sleeps = self._capture_sleeps(monkeypatch)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500, text="CUDA out of memory")
            return _ok_handler(request)

        ec = _client_with(handler)
        assert ec.embed(["a"]) == [[0.0]]
        assert calls["n"] == 3
        assert sleeps == [1.0, 2.0]
        ec.close()

    def test_gives_up_after_max_retries(self, monkeypatch):
        sleeps = self._capture_sleeps(monkeypatch)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500, text="CUDA out of memory")

        ec = _client_with(handler)
        with pytest.raises(httpx.HTTPStatusError):
            ec.embed(["a"])
        assert calls["n"] == 4  # 1 initial attempt + 3 retries
        assert sleeps == [1.0, 2.0, 4.0]
        ec.close()

    def test_4xx_is_not_retried(self, monkeypatch):
        sleeps = self._capture_sleeps(monkeypatch)
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, text="bad request")

        ec = _client_with(handler)
        with pytest.raises(httpx.HTTPStatusError):
            ec.embed(["a"])
        assert calls["n"] == 1
        assert sleeps == []
        ec.close()


class TestLifecycleAndRepr:
    def test_context_manager_closes_client(self):
        ec = _client_with(_ok_handler)
        with ec as c:
            assert c is ec
        assert ec._client.is_closed

    def test_repr_mentions_base_and_model(self):
        r = repr(GigaEmbeddingsClient())
        assert r.startswith("GigaEmbeddingsClient(") and "base=" in r and "model=" in r


class TestBatchSplittingOn503:
    """Once the retries are spent, a 503 means the batch is too big rather than too early:
    the vectorizer has already hunted for VRAM and queued the job on its own side. Halving
    is the one lever the client has that the server doesn't — the server must answer for
    the batch it was sent, we get to send a smaller one."""

    @staticmethod
    def _no_sleep(monkeypatch) -> None:
        monkeypatch.setattr(
            "src.api_clients.embeddings_client.time.sleep", lambda _seconds: None
        )

    def test_splits_the_batch_until_it_fits(self, monkeypatch):
        self._no_sleep(monkeypatch)
        sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            size = len(json.loads(request.content)["input"])
            sizes.append(size)
            if size > 2:
                return httpx.Response(503, text="GPU out of memory")
            return _ok_handler(request)

        ec = _client_with(handler)

        assert ec.embed(["a", "b", "c", "d"]) == [[0.0], [1.0], [0.0], [1.0]]
        assert sizes[0] == 4  # the whole batch, retried before being split
        assert sizes[-2:] == [2, 2]  # then one halving was enough
        ec.close()

    def test_a_single_text_is_never_split(self, monkeypatch):
        """Nothing left to halve — and the vectorizer splits an oversized text its side."""
        self._no_sleep(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="GPU out of memory")

        ec = _client_with(handler)

        with pytest.raises(httpx.HTTPStatusError):
            ec.embed(["a"])
        ec.close()

    def test_other_5xx_are_not_split(self, monkeypatch):
        """A 500 is the server failing, not a verdict that the batch is too large."""
        self._no_sleep(monkeypatch)
        sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sizes.append(len(json.loads(request.content)["input"]))
            return httpx.Response(500, text="boom")

        ec = _client_with(handler)

        with pytest.raises(httpx.HTTPStatusError):
            ec.embed(["a", "b"])
        assert set(sizes) == {2}  # retried whole, never halved
        ec.close()
