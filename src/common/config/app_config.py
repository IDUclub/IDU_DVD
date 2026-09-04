"""Application configuration (pydantic-settings).

Every value can be overridden via environment variables with the ``DVD_`` prefix or through ``.env``.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Chat providers the app knows how to build (see ``create_llm``). Anything else is a
# configuration error, not a reason to fall back to whichever one happens to be first.
LLM_PROVIDERS: frozenset[str] = frozenset({"openai", "ollama"})


def _str_list(value: Any) -> Any:
    """Parse a list-of-strings setting from JSON, from a comma-separated string, or from a list.

    The repo-wide convention for list fields is JSON (``'["a","b"]'``), and it still holds here,
    but a CORS origin list is written by whoever deploys the service rather than by whoever wrote
    the code, and ``DVD_CORS_ALLOW_ORIGINS=http://a,http://b`` is the form they reach for first.
    On a strictly-JSON field that costs a startup crash on a JSON parse error — a hard way to
    learn a quoting rule — so both forms are accepted.
    """
    if isinstance(value, str):
        raw = value.strip()
        value = json.loads(raw) if raw.startswith("[") else raw.split(",")
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return value


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

    service_auth_server_url: str
    service_auth_realm: str
    service_auth_client_id: str
    service_auth_client_secret: SecretStr
    # Realm role a *user* must hold to change the shared corpus and to open the admin panel.
    # Service accounts are trusted by virtue of holding client credentials and are not asked
    # for it.
    admin_role: str = "ADMIN"
    # IDU auth helper: the login proxy the admin panel posts credentials to (gMART uses the
    # same service behind its /auth/token). Unset means nobody can log into the panel — there
    # is no local password to fall back on.
    auth_helper_url: str | None = None
    auth_helper_api_key: SecretStr | None = None
    auth_helper_timeout: float = 15.0

    # --- Ollama (LLM for markup/tags; embeddings fallback provider) ---
    ollama_base: str = "http://a.dgx:11434"
    ollama_model: str = "gpt-oss:20b"  # structure markup, merge, tags, version
    ollama_embed_model: str = "bge-m3"  # vectorizer for provider="ollama" (1024-d)
    ollama_num_ctx: int = 16384
    ollama_num_predict: int = 8192
    ollama_timeout: float = 600.0

    # --- LLM provider (structure markup, merge, tags, version/head, references) ---
    # "openai" — any OpenAI-compatible /v1 endpoint (vLLM, LM Studio, llama.cpp, Ollama's own
    # /v1 shim, the OpenAI API), configured by the llm_* settings below; "ollama" — native
    # /api/chat off DVD_OLLAMA_* alone, kept for local development. Structured output is
    # requested per provider protocol (OpenAI `response_format=json_schema`, Ollama `format`)
    # — the schemas are unchanged. An unrecognized value is refused at startup rather than
    # quietly resolved to a provider nobody configured.
    llm_provider: str = "openai"
    # No default on purpose. The address of the LLM is deployment-specific and there is no
    # value that is right anywhere: a `localhost` default in particular is a trap, because
    # inside a container localhost is the container itself, so it fails with "connection
    # refused" against a perfectly healthy server on the host. Must be the /v1 root and must
    # be reachable *from the app* (in Docker: a service name, host.docker.internal, or an
    # explicit host).
    llm_base_url: str = ""
    llm_model: str = "gpt-oss-20b"
    llm_api_key: str | None = None  # optional: local servers ignore it
    # Response budget, the OpenAI counterpart of ollama_num_predict. There is no
    # OpenAI equivalent of num_ctx: the context window is whatever the server was
    # started with (vLLM --max-model-len), so size it there, not here.
    llm_max_tokens: int = 8192
    llm_timeout: float = 600.0

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
    # Advisory fallback only: the real dimension is probed from the active vectorizer at
    # startup (see ``probe_embedding_dim`` / ``init_dependencies``) and this value is
    # overwritten to match, so the Qdrant collection can never disagree with the model. It is
    # used verbatim only when the vectorizer is unreachable at boot (giga = 2048, bge-m3 = 1024).
    vector_size: int = 2048
    embed_batch: int = 32
    # Qdrant's HTTP API rejects a request over its configured max size (default 32 MiB).
    # A single ingest sends every node of one document in one upsert; large documents (long
    # SP-style regulations, big tables) can push that request past the limit on their own, so
    # points are chunked client-side rather than sent as one call — see QdrantRepository.upsert.
    qdrant_upsert_batch_size: int = 128
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

    # --- Urban API (territories + scenario -> project compatibility lookup) ---
    # A mandatory dependency: an empty URL fails fast at startup (see _require_urban_api).
    # A *runtime* outage never blocks ingestion — the document is indexed with
    # ``tagging_status="pending"`` and the backfill job fills the tags in later.
    urban_api_url: str = "https://urban-api.testing.idulab.ru"
    urban_api_timeout: float = 10.0
    urban_api_token: str | None = None  # Bearer token for private project scenarios

    # --- Tagging backfill (documents left pending by an outage or an unclear document) ---
    # The sweep runs shortly after startup (not during it — it makes LLM and HTTP calls),
    # then on a timer, and on demand from the admin panel. The attempt cap keeps a document
    # only a human can resolve from burning LLM time forever.
    tagging_backfill_delay: float = 30.0  # seconds after startup before the first sweep
    tagging_backfill_interval: float = (
        3600.0  # seconds between sweeps; 0 disables the timer
    )
    tagging_max_attempts: int = 5

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
    semantic_merge_max_passes: int = 1
    split_sentences: bool = True
    sent_min_len: int = 300

    # Independent LLM windows inside one document. The contour vLLM handles concurrent
    # requests efficiently; result order is preserved before overlap reconciliation.
    llm_concurrency: int = 8

    # --- Ingestion queue ---
    # Uploads are not processed in the request that brought them: the job is appended to a
    # durable Redis queue and picked up by ``ingest_concurrency`` background workers. The
    # count is therefore the parallelism limit — how many documents may run the GPU-bound
    # pipeline (LLM markup/tags/refs + embeddings) at once. Default 1: a single GPU is the
    # bottleneck, so documents are serialized and a new one simply waits its turn in the
    # queue. Raise only with more GPU capacity (e.g. a second Ollama instance / card).
    ingest_concurrency: int = 1
    ingest_queue_key: str = "dvd:ingest:pending"  # Redis list of queued jobs (FIFO)
    ingest_inflight_key: str = (
        "dvd:ingest:inflight"  # claimed by a worker, not yet finished
    )
    ingest_dead_key: str = "dvd:ingest:dead"  # jobs that exhausted their attempts
    ingest_checkpoint_prefix: str = (
        "dvd:ingest:checkpoint"  # per-job identity checkpoint (retry cleanup)
    )
    ingest_poll_interval: float = 2.0  # seconds between queue checks when idle
    # Processing attempts before a job is dead-lettered. Every restart mid-processing costs
    # one attempt, so this also caps how often a document that reliably kills the process can
    # take the whole service down with it on boot.
    ingest_max_attempts: int = 3
    # Object key prefix for direct-ingestion payloads parked in MinIO while queued (the direct
    # path has no uploaded file to fall back on). Removed once the job finishes.
    ingest_payload_prefix: str = "queue"

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

    # --- MinIO (original source files, closed contour — proxied via this service) ---
    # ``host:port`` — the minio SDK prepends the scheme itself (from ``minio_secure``) and
    # rejects an endpoint that carries one. A scheme may still be written here for symmetry
    # with the other service URLs: ``_normalize_minio_endpoint`` strips it and derives
    # ``minio_secure`` from it.
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket_documents: str = "dvd-documents"  # shared/regular document corpus
    minio_bucket_user_documents: str = "dvd-user-documents"  # all user document indices

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

    # --- CORS ---
    cors_allow_origins: Annotated[list[str], NoDecode] = ["*"]
    cors_allow_origin_regex: str | None = None
    cors_allow_credentials: bool = True
    cors_allow_methods: Annotated[list[str], NoDecode] = ["*"]
    cors_allow_headers: Annotated[list[str], NoDecode] = ["*"]
    cors_expose_headers: Annotated[list[str], NoDecode] = [
        "X-Request-ID",
        "Content-Disposition",
    ]
    cors_max_age: int = 600

    # --- Logging ---
    # Logs are written as JSON lines to a single growing file (filterable by date /
    # request_id via /system/logs) and as human-readable lines to stdout.
    log_dir: str = "./logs"
    log_file: str = "app.log"
    log_level: str = "INFO"

    @field_validator(
        "cors_allow_origins",
        "cors_allow_methods",
        "cors_allow_headers",
        "cors_expose_headers",
        mode="before",
    )
    @classmethod
    def _parse_str_list(cls, value: Any) -> Any:
        """Accept both the JSON and the comma-separated form for the CORS list settings."""
        return _str_list(value)

    @field_validator("cors_allow_origins", mode="after")
    @classmethod
    def _normalize_origins(cls, value: list[str]) -> list[str]:
        """Drop a trailing slash from every origin.

        A browser sends ``Origin: http://host:3000`` — never with a path, never with a trailing
        slash — and Starlette compares the header verbatim, so a configured
        ``http://host:3000/`` matches nothing and the request is refused with no CORS headers
        and no hint as to why.
        """
        return [origin.rstrip("/") for origin in value]

    @model_validator(mode="before")
    @classmethod
    def _normalize_minio_endpoint(cls, data):
        """Accept ``minio_endpoint`` with or without a scheme.

        Unlike ``qdrant_url`` / ``redis_url``, the minio SDK wants a bare ``host:port`` and
        raises ``ValueError: path in endpoint is not allowed`` for anything else — writing the
        familiar ``http://host:9000`` there used to crash the app at startup. So a scheme is
        accepted and stripped here, and, when ``minio_secure`` was not configured explicitly,
        it decides the transport (``https`` -> secure).
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("minio_endpoint")
        if not isinstance(raw, str):
            return data

        endpoint = raw.strip()
        scheme = ""
        if "://" in endpoint:
            scheme, _, endpoint = endpoint.partition("://")
            scheme = scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"DVD_MINIO_ENDPOINT: unsupported scheme {scheme!r} — "
                    "expected http:// or https:// (or a bare host:port)"
                )
        endpoint = endpoint.rstrip("/")
        if "/" in endpoint:
            raise ValueError(
                f"DVD_MINIO_ENDPOINT: a path in the endpoint is not allowed ({raw!r}) — "
                "expected host:port"
            )

        data["minio_endpoint"] = endpoint
        # An explicit DVD_MINIO_SECURE always wins; the scheme only fills the gap.
        if scheme and "minio_secure" not in data:
            data["minio_secure"] = scheme == "https"
        return data

    @model_validator(mode="after")
    def _require_urban_api(self) -> "Settings":
        """Refuse to start without an Urban API URL — territory tagging has no fallback.

        Document level and territory are derived from the Urban API territory tree, and there
        is no offline mode: booting without it would silently degrade every ingest to
        ``pending`` forever. A runtime outage is a different matter and is tolerated.
        """
        if not (self.urban_api_url or "").strip():
            raise ValueError(
                "DVD_URBAN_API_URL: обязательный параметр — Urban API поставляет иерархию "
                "территорий для тегирования документов (например "
                "https://urban-api.testing.idulab.ru)"
            )
        return self

    @model_validator(mode="after")
    def _require_llm_endpoint(self) -> "Settings":
        """Refuse to start on an LLM the app cannot reach, or on a provider it does not know.

        Both failures used to be silent and both cost a corpus. An unknown ``llm_provider``
        fell through to Ollama, so a typo (or an override that never arrived) sent the pipeline
        to a service nobody had configured; and with the LLM unreachable every markup window is
        skipped, which does not fail the ingest — it indexes the document unstructured, under
        ``name="unknown"``, and every later document then attaches to it as another *version*
        of that same phantom document. Catching it here, at boot, is the only place where the
        damage is still zero.
        """
        provider = (self.llm_provider or "").strip().lower()
        if provider not in LLM_PROVIDERS:
            raise ValueError(
                f"DVD_LLM_PROVIDER: неизвестный провайдер '{self.llm_provider}' — "
                f"допустимо: {', '.join(sorted(LLM_PROVIDERS))}"
            )
        self.llm_provider = provider
        if provider == "openai":
            base = (self.llm_base_url or "").strip().rstrip("/")
            if not base:
                raise ValueError(
                    "DVD_LLM_BASE_URL: обязательный параметр при DVD_LLM_PROVIDER=openai — "
                    "корень /v1 OpenAI-совместимого сервера, достижимый из приложения "
                    "(например http://a.dgx:8010/v1; в Docker localhost — это сам контейнер)"
                )
            self.llm_base_url = base
        return self

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
