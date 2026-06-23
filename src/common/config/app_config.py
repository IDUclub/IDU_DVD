"""Application configuration (pydantic-settings).

Every value can be overridden via environment variables with the ``DVD_`` prefix or through ``.env``.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration class: all tunable parameters in one place."""

    model_config = SettingsConfigDict(
        env_prefix="DVD_", env_file=".env", extra="ignore"
    )

    # --- Ollama (LLM for markup/tags + embeddings) ---
    ollama_base: str = "http://a.dgx:11434"
    ollama_model: str = "gpt-oss:20b"  # structure markup, merge, tags, version
    ollama_embed_model: str = "bge-m3"  # vectorizer (1024-d, multilingual)
    ollama_num_ctx: int = 16384
    ollama_num_predict: int = 8192
    ollama_timeout: float = 600.0

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "documents"
    vector_size: int = 1024  # MUST match ollama_embed_model
    embed_batch: int = 32

    # --- Redis (parsing job status + document/version registry) ---
    redis_url: str = "redis://localhost:6379/0"
    redis_job_ttl: int = 86400  # seconds, job status TTL

    # --- Search ---
    search_limit: int = 10
    max_context_height: int = 6  # cap on context height (neighbours before/after)

    # --- Parser pipeline (ported from notebooks/parser.ipynb) ---
    partition_strategy: str = "hi_res"  # 'fast' — for text formats without OCR
    languages: list[str] = ["rus", "eng"]
    window_chars: int = 6000
    window_max_items: int = 22  # max parts per Stage-2 window (structured output)
    overlap_blocks: int = 3
    semantic_merge_max_passes: int = 2
    split_sentences: bool = True
    sent_min_len: int = 300

    # --- Upload ---
    upload_dir: str = "./_uploads"
    allowed_extensions: list[str] = [".docx"]  # docx only for now; other formats later

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"ollama={self.ollama_base} model={self.ollama_model} embed={self.ollama_embed_model}, "
            f"qdrant={self.qdrant_url} collection={self.qdrant_collection} "
            f"vector_size={self.vector_size}, "
            f"redis={self.redis_url})"
        )


settings = Settings()
