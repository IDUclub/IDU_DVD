"""Integration fixtures: probe the live local stack and skip cleanly when a service is down.

These tests talk to the real Qdrant + Redis (Docker Compose) and the local Ollama. They are
marked ``integration`` (see pyproject) and each ``require_*`` fixture skips the test if its
service is unavailable, so ``make test`` (unit only) and partial stacks never produce failures.
"""

from __future__ import annotations

import uuid

import pytest

from src.api_clients import OllamaClient, create_embedder
from src.common.config import Settings
from src.common.db.redis_client import RedisClient


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """Settings resolved from env — pointed at localhost by the root conftest."""
    return Settings()


@pytest.fixture
def require_redis(live_settings) -> RedisClient:
    client = RedisClient(live_settings)
    if not client.ping():
        pytest.skip("Redis unavailable on the local stack")
    return client


@pytest.fixture
def require_qdrant(live_settings):
    from qdrant_client import QdrantClient

    try:
        QdrantClient(
            url=live_settings.qdrant_url, api_key=live_settings.qdrant_api_key
        ).get_collections()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant unavailable on the local stack: {exc}")


@pytest.fixture
def require_ollama():
    client = OllamaClient()
    if not client.available():
        client.close()
        pytest.skip("Ollama unavailable on localhost:11434")
    yield client
    client.close()


@pytest.fixture
def require_embedder(live_settings):
    """The configured embeddings provider (giga-vectorizer or Ollama), or skip."""
    client = create_embedder()
    if not client.available():
        client.close()
        pytest.skip(
            f"embeddings provider '{live_settings.embeddings_provider}' unavailable"
        )
    yield client
    client.close()


@pytest.fixture
def temp_collection(live_settings, require_qdrant) -> Settings:
    """A throwaway Qdrant collection; dropped after the test.

    Namespacing appends a ``__{model}_{dim}`` suffix (and tests may vary ``vector_size``),
    so cleanup drops every collection whose name starts with the unique base and clears the
    scoped Redis registry keys (best-effort — Redis may be absent for Qdrant-only tests).
    """
    name = f"itest_{uuid.uuid4().hex[:8]}"
    s = live_settings.model_copy(update={"qdrant_collection": name})
    yield s
    from qdrant_client import QdrantClient

    try:
        client = QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key)
        for coll in client.get_collections().collections:
            if coll.name == name or coll.name.startswith(f"{name}__"):
                client.delete_collection(coll.name)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    try:
        r = RedisClient(s)
        if r.ping():
            for key in r.r.scan_iter(match=f"{s.registry_prefix}:*"):
                r.r.delete(key)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def reset_dependencies():
    """Keep the Dependencies singleton from leaking between integration tests."""
    from src.dependencies import Dependencies

    Dependencies.reset()
    yield
    Dependencies.reset()
