"""Integration: src/api_clients against the local Ollama (localhost:11434).

Verifies real embeddings (dimension matches the configured vector size) and strict-JSON chat.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


class TestOllamaIntegration:
    def test_embed_dimension_matches_config(self, require_ollama, live_settings):
        vectors = require_ollama.embed(["проверка эмбеддинга"])
        assert len(vectors) == 1
        assert len(vectors[0]) == live_settings.vector_size  # bge-m3 == 1024

    def test_chat_returns_valid_json(self, require_ollama):
        data = require_ollama.chat(
            "Верни поле answer со строковым значением. Ответ короткий.",
            "Скажи что-нибудь.",
            _ANSWER_SCHEMA,
        )
        assert isinstance(data, dict) and isinstance(data.get("answer"), str)
