"""Embeddings client for an OpenAI-compatible vectorizer service (giga-vectorizer).

The service exposes ``POST /v1/embeddings`` (OpenAI schema plus an optional ``prompt``
extension — a per-request instruction prefix) and ``GET /health``. Giga-Embeddings-instruct
is asymmetric: queries are embedded with an instruction prefix, documents without one, so
the client offers explicit ``embed_documents`` / ``embed_query`` helpers.

``create_embedder`` picks the configured provider (``giga`` or ``ollama``); both clients
share the same embedding surface, so the pipeline code does not care which one it got.
"""

from __future__ import annotations

import time

import httpx
import structlog

from src.api_clients.ollama_client import OllamaClient
from src.common.config import settings

log = structlog.get_logger(__name__)

# The shared GPU embeddings service can return a transient 500 under load (e.g. a
# CUDA OOM on a contended GPU, see the a.dgx:8010 incident) that clears up once the
# in-flight batch on the other side finishes. Retry those a few times with backoff
# before giving up; 4xx responses mean the request itself is wrong and are never retried.
_EMBEDDINGS_MAX_RETRIES = 3
_EMBEDDINGS_BACKOFF_BASE_SECONDS = 1.0


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
        resp = self._post_with_retry(body)
        data = resp.json().get("data")
        if not data:
            raise EmbeddingsError(
                "Сервис эмбеддингов не вернул data: " + resp.text[:200]
            )
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]

    def _post_with_retry(self, body: dict) -> httpx.Response:
        """POST /v1/embeddings, retrying transient 5xx responses with backoff.

        Up to ``_EMBEDDINGS_MAX_RETRIES`` retries (4 attempts total), sleeping
        1s / 2s / 4s between them. A 4xx is raised immediately — retrying a
        malformed request wouldn't help.
        """
        attempt = 0
        while True:
            resp = self._client.post(self.base + "/v1/embeddings", json=body)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp

            attempt += 1
            if attempt > _EMBEDDINGS_MAX_RETRIES:
                resp.raise_for_status()

            delay = _EMBEDDINGS_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "embeddings_request_retrying",
                status_code=resp.status_code,
                attempt=attempt,
                delay=delay,
            )
            time.sleep(delay)

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
