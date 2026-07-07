"""API clients package: thin synchronous clients for external services used by the pipeline.

Implementations live in dedicated modules (e.g. ``ollama_client``); this file only marks the
package and re-exports the public symbols.
"""

from src.api_clients.embeddings_client import (  # noqa: F401
    EmbeddingsError,
    GigaEmbeddingsClient,
    create_embedder,
)
from src.api_clients.ollama_client import OllamaClient, OllamaError  # noqa: F401

__all__ = [
    "EmbeddingsError",
    "GigaEmbeddingsClient",
    "OllamaClient",
    "OllamaError",
    "create_embedder",
]
