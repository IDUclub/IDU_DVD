"""Embeddings client for an OpenAI-compatible vectorizer service (giga-vectorizer).

The service exposes ``POST /v1/embeddings`` (OpenAI schema plus an optional ``prompt``
extension — a per-request instruction prefix) and ``GET /health``. Giga-Embeddings-instruct
is asymmetric: queries are embedded with an instruction prefix, documents without one, so
the client offers explicit ``embed_documents`` / ``embed_query`` helpers.

``create_embedder`` picks the configured provider (``giga`` or ``ollama``); both clients
share the same embedding surface, so the pipeline code does not care which one it got.
"""

from __future__ import annotations

import httpx
import structlog

from src.api_clients.ollama_client import OllamaClient
from src.common.config import settings

log = structlog.get_logger(__name__)


class EmbeddingsError(RuntimeError):
    pass


class GigaEmbeddingsClient:
    """Synchronous client for the giga-vectorizer embeddings service.

    Used by the background ingestion (which runs in a threadpool), hence synchronous —
    same convention as ``OllamaClient``.
    """

    def __init__(
        self,
        base: str | None = None,
        model: str | None = None,
        query_prompt: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base = (base or settings.embeddings_url).rstrip("/")
        self.model = model or settings.embeddings_model
        self.query_prompt = (
            query_prompt
            if query_prompt is not None
            else settings.embeddings_query_prompt
        )
        self.timeout = timeout or settings.embeddings_timeout
        self._client = httpx.Client(timeout=self.timeout)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base={self.base}, model={self.model})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GigaEmbeddingsClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def available(self) -> bool:
        try:
            self._client.get(self.base + "/health", timeout=5).raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("embeddings_service_unavailable", error=str(exc))
            return False

    def embed(self, texts: list[str], prompt: str | None = None) -> list[list[float]]:
        """Embed ``texts``; ``prompt`` is prepended to each one by the service.

        ``None`` defers to the service's ``VECTOR_DEFAULT_PROMPT``; an empty string
        explicitly disables the prefix.
        """
        body: dict = {"input": texts, "model": self.model}
        if prompt is not None:
            body["prompt"] = prompt
        resp = self._client.post(self.base + "/v1/embeddings", json=body)
        resp.raise_for_status()
        data = resp.json().get("data")
        if not data:
            raise EmbeddingsError(
                "Сервис эмбеддингов не вернул data: " + resp.text[:200]
            )
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Document embeddings: explicitly no instruction prefix."""
        return self.embed(texts, prompt="")

    def embed_query(self, text: str) -> list[float]:
        """Query embedding: instruction prefix from ``DVD_EMBEDDINGS_QUERY_PROMPT``."""
        return self.embed([text], prompt=self.query_prompt)[0]


def create_embedder() -> GigaEmbeddingsClient | OllamaClient:
    """Vectorizer for the configured provider (``DVD_EMBEDDINGS_PROVIDER``)."""
    if settings.embeddings_provider == "ollama":
        return OllamaClient()
    return GigaEmbeddingsClient()


def probe_embedding_dim() -> int | None:
    """Actual vector dimension of the active vectorizer, or ``None`` if it is unreachable.

    Embeds a single throwaway string and measures the result, so the Qdrant collection is
    always created to match the model rather than a hand-set ``DVD_VECTOR_SIZE``. A failure
    here is non-fatal: the caller keeps the configured fallback and logs it.
    """
    try:
        with create_embedder() as embedder:
            vectors = embedder.embed_documents(["dimension probe"])
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding_dim_probe_failed", error=str(exc))
        return None
    if not vectors or not vectors[0]:
        log.warning("embedding_dim_probe_empty")
        return None
    return len(vectors[0])
