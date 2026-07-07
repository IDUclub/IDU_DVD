"""Ollama client: chat with a strict JSON schema (structure/tags) and embeddings (vectorizer)."""

from __future__ import annotations

import json

import httpx
import structlog

from src.common.config import settings

log = structlog.get_logger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    """Synchronous client for a local/remote Ollama instance.

    Used by the background ingestion (which runs in a threadpool), hence synchronous —
    as in the original notebook.
    """

    def __init__(
        self,
        base: str | None = None,
        model: str | None = None,
        embed_model: str | None = None,
        num_ctx: int | None = None,
        num_predict: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base = (base or settings.ollama_base).rstrip("/")
        self.model = model or settings.ollama_model
        self.embed_model = embed_model or settings.ollama_embed_model
        self.num_ctx = num_ctx or settings.ollama_num_ctx
        self.num_predict = num_predict or settings.ollama_num_predict
        self.timeout = timeout or settings.ollama_timeout
        self._client = httpx.Client(timeout=self.timeout)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base={self.base}, model={self.model}, "
            f"embed_model={self.embed_model})"
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def available(self) -> bool:
        try:
            self._client.get(self.base + "/api/tags", timeout=5).raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("ollama_unavailable", error=str(exc))
            return False

    def chat(
        self, system: str, user: str, schema: dict, model: str | None = None
    ) -> dict:
        resp = self._client.post(
            self.base + "/api/chat",
            json={
                "model": model or self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "num_ctx": self.num_ctx,
                    "num_predict": self.num_predict,
                },
            },
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        if not content.strip():
            # gpt-oss sometimes returns empty content with a filled "thinking" field
            raise OllamaError("Пустой ответ Ollama: " + resp.text[:200])
        return json.loads(content)

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        resp = self._client.post(
            self.base + "/api/embed",
            json={"model": model or self.embed_model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        vectors = data.get("embeddings")
        if not vectors:
            raise OllamaError("Ollama не вернул embeddings: " + resp.text[:200])
        return vectors

    # bge-m3 is symmetric: documents and queries are embedded identically. The split
    # exists to mirror GigaEmbeddingsClient so the pipeline is provider-agnostic.
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]
