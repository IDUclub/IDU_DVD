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

    # --- Reference linking (extract links to other documents/clauses, resolve against the store) ---
    enable_reference_linking: bool = True
    ref_pattern_learning: bool = (
        False  # let the LLM grow the regex base (self-improvement)
    )
    ref_pattern_collection: str = (
        "ref_patterns"  # Qdrant collection for learned patterns
    )

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
    # Lightweight, OCR-free formats handled by unstructured. Scanned PDF/OCR is deferred
    # (heavy torch/poppler/tesseract backends); add ".pdf" once those are provisioned.
    allowed_extensions: list[str] = [".docx", ".txt", ".md", ".html", ".htm"]

    # --- Document identity defaults (general-purpose corpus metadata) ---
    # Generic fallbacks for the cross-service payload fields when the uploader omits them.
    # Domain consumers (e.g. MSI-TSIM) override per upload via form fields / external_ids.
    default_doc_type: str = (
        "document"  # document | regulation | article | book | web | …
    )
    default_corpus: str = "default"  # logical corpus/namespace a document belongs to
    default_lang: str | None = None  # ISO-639 code; None = unknown / not detected

    # --- Kafka (document-processed events via otteroad) ---
    # Publishing is optional: it stays off until a broker is configured
    # (empty/None bootstrap servers = disabled).
    kafka_bootstrap_servers: str | None = None  # e.g. "kafka:9092"; None = disabled
    kafka_schema_registry_url: str = (
        "https://schema-registry.next.idulab.ru"  # AVRO Schema Registry (IDU contour)
    )
    kafka_client_id: str = "idu-dvd"
    kafka_outbox_key: str = "dvd:kafka:outbox"  # Redis list of pending events
    kafka_dead_letter_key: str = (
        "dvd:kafka:outbox:dead"  # events that exhausted retries
    )
    kafka_poll_interval: float = 1.0  # seconds between outbox checks when idle
    kafka_retry_interval: float = 5.0  # seconds to wait after a failed send
    kafka_max_attempts: int = 10  # send attempts before an event is dead-lettered

    # --- Logging ---
    # Logs are written as JSON lines to a single growing file (filterable by date /
    # request_id via /system/logs) and as human-readable lines to stdout.
    log_dir: str = "./logs"
    log_file: str = "app.log"
    log_level: str = "INFO"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"ollama={self.ollama_base} model={self.ollama_model} embed={self.ollama_embed_model}, "
            f"qdrant={self.qdrant_url} collection={self.qdrant_collection} "
            f"vector_size={self.vector_size}, "
            f"redis={self.redis_url})"
        )


settings = Settings()
