"""Integration: src/api_clients against the live giga-vectorizer (localhost:8001).

Verifies real embeddings (dimension matches the configured vector size) and that the
query instruction prompt actually changes the vector (the model is asymmetric).
Self-skips when the vectorizer is down or the configured provider is not "giga".
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestGigaEmbeddingsIntegration:
    def test_embed_dimension_matches_config(self, require_embedder, live_settings):
        if live_settings.embeddings_provider != "giga":
            pytest.skip("configured provider is not giga")
        vectors = require_embedder.embed_documents(["проверка эмбеддинга"])
        assert len(vectors) == 1
        assert len(vectors[0]) == live_settings.vector_size  # Giga == 2048

    def test_query_prompt_changes_the_vector(self, require_embedder, live_settings):
        if live_settings.embeddings_provider != "giga":
            pytest.skip("configured provider is not giga")
        text = "какая максимальная высота жилого дома?"
        doc_vec = require_embedder.embed_documents([text])[0]
        query_vec = require_embedder.embed_query(text)
        assert len(doc_vec) == len(query_vec)
        assert doc_vec != query_vec  # instruction prefix must shift the embedding
