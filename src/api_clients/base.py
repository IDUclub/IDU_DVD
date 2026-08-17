"""Shared contracts for the LLM clients.

The pipeline (structure markup, merge, tagging, version/head detection, reference extraction)
needs exactly one thing from a language model: **one blocking call that returns JSON matching a
given schema**. That surface is captured by :class:`ChatClient`, so the stages never care which
backend answered them.

The error base lives here too: :class:`OllamaError` and :class:`LlmError` subclasses let a caller
catch "the model failed" without naming a provider.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LlmError(RuntimeError):
    """A language-model call failed (transport ok, answer unusable)."""


@runtime_checkable
class ChatClient(Protocol):
    """One strict-JSON chat call, plus the lifecycle the pipeline relies on.

    Implemented by :class:`~src.api_clients.ollama_client.OllamaClient` (native Ollama
    ``/api/chat``) and :class:`~src.api_clients.llm_client.OpenAICompatibleClient`
    (``/v1/chat/completions`` — vLLM, LM Studio, llama.cpp, Ollama's own ``/v1`` shim, OpenAI).
    Both are synchronous: ingestion runs in a threadpool, so a blocking call is simpler and
    avoids nested event loops.
    """

    #: Model identifier used for the chat calls.
    model: str

    def chat(
        self, system: str, user: str, schema: dict, model: str | None = None
    ) -> dict:
        """Return the model's answer parsed as JSON, constrained to ``schema``."""

    def available(self) -> bool:
        """True when the backend answers a cheap liveness call."""

    def close(self) -> None:
        """Release pooled connections."""
