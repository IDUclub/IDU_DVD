"""OpenAI-compatible chat client for the markup/tagging LLM.

Speaks ``POST /v1/chat/completions`` against anything that implements the OpenAI protocol —
**vLLM**, LM Studio, llama.cpp's server, Ollama's own ``/v1`` shim, or the OpenAI API itself —
so the pipeline is no longer tied to a native Ollama endpoint. ``DVD_LLM_BASE_URL`` must point
at the ``/v1`` root.

Structured output uses ``response_format={"type": "json_schema", ...}``, the OpenAI-side
equivalent of Ollama's ``format=<schema>``: the server constrains decoding to the schema, so the
stages keep receiving a dict that already matches what they asked for.

``create_llm`` picks the configured provider (``ollama`` or ``openai``); both clients implement
:class:`~src.api_clients.base.ChatClient`, so pipeline code does not care which one it got.
"""

from __future__ import annotations

import json

import httpx
import structlog

from src.api_clients.base import ChatClient, LlmError
from src.api_clients.ollama_client import OllamaClient
from src.common.config import settings

log = structlog.get_logger(__name__)


class OpenAICompatibleClient:
    """Synchronous client for an OpenAI-compatible chat endpoint.

    Used by the background ingestion (which runs in a threadpool), hence synchronous —
    same convention as ``OllamaClient`` and the embeddings clients.
    """

    def __init__(
        self,
        base: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base = (base or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.max_tokens = max_tokens or settings.llm_max_tokens
        self.timeout = timeout or settings.llm_timeout
        self._client = httpx.Client(timeout=self.timeout)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base={self.base}, model={self.model})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenAICompatibleClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Local servers ignore the key; the OpenAI API and gated gateways require it.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def available(self) -> bool:
        try:
            self._client.get(
                self.base + "/models", headers=self._headers(), timeout=5
            ).raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_unavailable", base=self.base, error=str(exc))
            return False

    def chat(
        self, system: str, user: str, schema: dict, model: str | None = None
    ) -> dict:
        resp = self._client.post(
            self.base + "/chat/completions",
            headers=self._headers(),
            json={
                "model": model or self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "dvd_response",
                        "schema": schema,
                        "strict": True,
                    },
                },
            },
        )
        resp.raise_for_status()
        choice = (resp.json().get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""

        # Truncation must not pass silently. Constrained decoding keeps the prefix valid, so a
        # cut-off answer can still parse as JSON while missing entries the caller will read as
        # "the model saw nothing here" — losing document nodes instead of failing the window.
        if choice.get("finish_reason") == "length":
            raise LlmError(
                f"Ответ LLM обрезан по лимиту токенов (max_tokens={self.max_tokens}): "
                + content[:200]
            )

        if not content.strip():
            # Reasoning models (gpt-oss) can spend the whole budget on the reasoning trace and
            # return empty content — same failure Ollama's /api/chat shows with "thinking".
            if message.get("reasoning_content"):
                raise LlmError(
                    "LLM вернул только reasoning без содержимого: "
                    + str(message["reasoning_content"])[:200]
                )
            raise LlmError("Пустой ответ LLM: " + resp.text[:200])

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM вернул невалидный JSON: {content[:200]}") from exc


def create_llm() -> ChatClient:
    """LLM client for the configured provider (``DVD_LLM_PROVIDER``)."""
    if settings.llm_provider == "openai":
        return OpenAICompatibleClient()
    return OllamaClient()
