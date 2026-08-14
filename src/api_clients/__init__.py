"""API clients package: thin synchronous clients for external services used by the pipeline.

Implementations live in dedicated modules (e.g. ``ollama_client``); this file only marks the
package and re-exports the public symbols.
"""

from src.api_clients.embeddings_client import (  # noqa: F401
    EmbeddingsError,
    GigaEmbeddingsClient,
    create_embedder,
    probe_embedding_dim,
)
from src.api_clients.ollama_client import OllamaClient, OllamaError  # noqa: F401
from src.api_clients.urban_api_client import (  # noqa: F401
    COUNTRY_TERRITORY_ID,
    DOCUMENT_LEVELS,
    LEVEL_FEDERAL,
    LEVEL_MUNICIPAL,
    LEVEL_REGIONAL,
    Territory,
    TerritoryNotFound,
    UrbanApiClient,
    UrbanApiError,
    normalized_level,
)

__all__ = [
    "COUNTRY_TERRITORY_ID",
    "DOCUMENT_LEVELS",
    "LEVEL_FEDERAL",
    "LEVEL_MUNICIPAL",
    "LEVEL_REGIONAL",
    "EmbeddingsError",
    "GigaEmbeddingsClient",
    "OllamaClient",
    "OllamaError",
    "Territory",
    "TerritoryNotFound",
    "UrbanApiClient",
    "UrbanApiError",
    "create_embedder",
    "normalized_level",
    "probe_embedding_dim",
]
