"""API clients package: thin synchronous clients for external services used by the pipeline.

Implementations live in dedicated modules (e.g. ``ollama_client``); this file only marks the
package and re-exports the public symbols.
"""

from src.api_clients.auth_helper_client import (  # noqa: F401
    AuthHelperClient,
    AuthHelperError,
)
from src.api_clients.base import ChatClient, LlmError  # noqa: F401
from src.api_clients.embeddings_client import (  # noqa: F401
    EmbeddingsError,
    GigaEmbeddingsClient,
    create_embedder,
    probe_embedding_dim,
)
from src.api_clients.llm_client import (  # noqa: F401
    OpenAICompatibleClient,
    create_llm,
)
from src.api_clients.ollama_client import OllamaClient, OllamaError  # noqa: F401
from src.api_clients.urban_api_client import (  # noqa: F401
    COUNTRY_TERRITORY_ID,
    DOCUMENT_LEVELS,
    LEVEL_FEDERAL,
    LEVEL_MUNICIPAL,
    LEVEL_REGIONAL,
    ScenarioNotFound,
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
    "AuthHelperClient",
    "AuthHelperError",
    "ChatClient",
    "EmbeddingsError",
    "GigaEmbeddingsClient",
    "LlmError",
    "OllamaClient",
    "OllamaError",
    "OpenAICompatibleClient",
    "Territory",
    "TerritoryNotFound",
    "ScenarioNotFound",
    "UrbanApiClient",
    "UrbanApiError",
    "create_embedder",
    "create_llm",
    "normalized_level",
    "probe_embedding_dim",
]
