"""Application configuration (pydantic-settings).

Every value can be overridden via environment variables with the ``DVD_`` prefix or through ``.env``.
"""

from __future__ import annotations

import re

from pydantic_settings import BaseSettings, SettingsConfigDict


def _slug(value: str) -> str:
    """Qdrant/Redis-safe token from a model name (drops the author prefix).

    ``ai-sage/Giga-Embeddings-instruct`` -> ``giga_embeddings_instruct``; ``bge-m3`` -> ``bge_m3``.
    """
    tail = value.rsplit("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9]+", "_", tail).strip("_")


class Settings(BaseSettings):
    """Application configuration class: all tunable parameters in one place."""

    model_config = SettingsConfigDict(
        env_prefix="DVD_", env_file=".env", extra="ignore"
    )

    # --- Ollama (LLM for markup/tags; embeddings fallback provider) ---
    ollama_base: str = "http://a.dgx:11434"
    ollama_model: str = "gpt-oss:20b"  # structure markup, merge, tags, version
    ollama_embed_model: str = "bge-m3"  # vectorizer for provider="ollama" (1024-d)
    ollama_num_ctx: int = 16384
    ollama_num_predict: int = 8192
    ollama_timeout: float = 600.0

    # --- Embeddings provider (vectorizer) ---
    # "giga" — the GPU giga-vectorizer service (OpenAI-compatible /v1/embeddings,
    # Giga-Embeddings-instruct, 2048-d); "ollama" — legacy fallback via /api/embed.
    # Switching providers changes the vector space: vector_size must match and the
    # Qdrant collection must be re-indexed from scratch.
    embeddings_provider: str = "giga"
    embeddings_url: str = "http://localhost:8001"
    embeddings_model: str = "ai-sage/Giga-Embeddings-instruct"
    # Instruction prefix for query embeddings (the model is asymmetric: documents are
    # embedded without a prompt, queries with one).
    embeddings_query_prompt: str = (
        "Instruct: Дан вопрос, необходимо найти абзац текста с ответом\nQuery: "
    )
    embeddings_timeout: float = 600.0

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "documents"  # base name (see collection_namespacing)
    vector_size: int = (
        2048  # MUST match the provider model (giga = 2048, bge-m3 = 1024)
    )
    embed_batch: int = 32
    # When True (default), the physical collection — and its Redis registry namespace —
    # is derived from the base name + embedding model + dimension, so a change to the
    # embedding space provisions a brand-new collection at startup and leaves the previous
    # one untouched (safe provider switches / rollbacks; no manual re-index dance). When
    # False, `qdrant_collection` is used verbatim and a dimension mismatch on an existing
    # collection fails fast instead of silently writing into the wrong space.
    collection_namespacing: bool = True

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

    # --- Ingestion concurrency ---
    # How many documents may run the GPU-bound pipeline (LLM markup/tags/refs + embeddings)
    # at once. Default 1 — a single GPU is the bottleneck, so documents are serialized: a new
    # one waits (job status "queued") until the current one frees the GPU. Raise only with
    # more GPU capacity (e.g. a second Ollama instance / card).
    ingest_concurrency: int = 1

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

    @property
    def embedding_model_name(self) -> str:
        """Embedding model of the active provider (goes into ``embedding_meta``)."""
        if self.embeddings_provider == "ollama":
            return self.ollama_embed_model
        return self.embeddings_model

    @property
    def effective_collection(self) -> str:
        """Physical Qdrant collection actually used (see ``collection_namespacing``).

        Namespaced form: ``{base}__{model_slug}_{dim}`` — so distinct embedding spaces never
        share a collection and switching models lands in a fresh one.
        """
        if not self.collection_namespacing:
            return self.qdrant_collection
        return (
            f"{self.qdrant_collection}__{_slug(self.embedding_model_name)}"
            f"_{self.vector_size}"
        )

    @property
    def registry_prefix(self) -> str:
        """Redis key prefix for the document registry, scoped to the collection.

        Keeps dedup/version/name state in lockstep with the physical collection: a new
        embedding space gets a clean registry, so re-ingestion is never blocked by stale
        hashes from the previous space. Classic fixed mode keeps the legacy ``dvd`` prefix.
        """
        if not self.collection_namespacing:
            return "dvd"
        return f"dvd:{self.effective_collection}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"ollama={self.ollama_base} model={self.ollama_model}, "
            f"embeddings={self.embeddings_provider} model={self.embedding_model_name}, "
            f"qdrant={self.qdrant_url} collection={self.effective_collection} "
            f"vector_size={self.vector_size}, "
            f"redis={self.redis_url})"
        )


settings = Settings()
