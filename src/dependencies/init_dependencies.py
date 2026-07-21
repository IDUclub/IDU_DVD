"""Dependency initialization: building and wiring all modules at application startup.

The container declaration and getters live in ``dependencies``; this module only builds the
objects and stores them in the ``Dependencies`` singleton.
"""

from __future__ import annotations

import structlog
from minio import Minio

from src.api_clients import probe_embedding_dim
from src.broker.outbox import EventOutbox
from src.broker.publisher import KafkaPublisher
from src.common.config import Settings, settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import (
    DocumentRegistry,
    JobStore,
    RedisClient,
    UserIndexRegistry,
)
from src.common.logger import configure_logging
from src.dependencies.dependencies import Dependencies
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import VersionDetector
from src.dvd_service.services.dvd_service import (
    DocumentEditorService,
    DocumentsService,
    IngestionService,
    LibraryService,
    SearchService,
    TagsService,
)
from src.dvd_service.services.user_index_service import UserIndexService
from src.system_service.controllers import SystemController

log = structlog.get_logger(__name__)


def _warn_on_registry_divergence(
    qdrant: QdrantRepository, registry: DocumentRegistry, app_logger
) -> None:
    """Compare the Redis registry against the collection it describes and warn on drift.

    The two stores are written together (``register()`` runs after the upsert) and cleaned
    together, so registered names without points mean the pair drifted apart — a replaced
    Qdrant instance, a dropped collection, a changed ``DVD_QDRANT_COLLECTION``. Nothing is
    deleted here on purpose: a boot against a wrong/empty Qdrant would otherwise wipe a
    registry that is still perfectly valid for the right one. Ghost entries are repaired
    individually, on the upload that trips over them (``reject_duplicate``).
    """
    try:
        names = registry.names()
        if not names:
            return
        points = qdrant.count()
        if points == 0:
            app_logger.warning(
                "registry_diverged_from_collection",
                collection=qdrant.collection,
                registered_names=len(names),
                points=0,
                hint=(
                    "the registry describes documents the collection does not hold — check "
                    "DVD_QDRANT_URL / DVD_QDRANT_COLLECTION, or let uploads repair entries"
                ),
            )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never block startup
        app_logger.warning("registry_divergence_check_failed", error=str(exc))


def init_dependencies(s: Settings = settings) -> Dependencies:
    """Initialize and wire all modules, then store them in the ``Dependencies`` singleton.

    Called once at application startup (lifespan in ``src/main.py``).
    """
    # Configure logging first so everything below (and every module's logger) is captured
    # by the file + console sinks.
    configure_logging(s)
    app_logger = structlog.get_logger("app")

    # Pin the Qdrant vector size to whatever the active vectorizer actually returns, so the
    # collection dimension can never drift from the embedding model. Falls back to the
    # configured ``vector_size`` when the vectorizer is unreachable at boot.
    detected_dim = probe_embedding_dim()
    if detected_dim:
        if detected_dim != s.vector_size:
            app_logger.warning(
                "vector_size_autodetected",
                configured=s.vector_size,
                detected=detected_dim,
            )
        s.vector_size = detected_dim
    else:
        app_logger.warning("vector_size_probe_unavailable", fallback=s.vector_size)

    qdrant = QdrantRepository(s)
    qdrant.ensure_collection()
    if s.enable_reference_linking:
        qdrant.ensure_pattern_collection()
    redis = RedisClient(s)
    jobs = JobStore(redis)
    registry = DocumentRegistry(redis, prefix=s.registry_prefix)
    user_index_registry = UserIndexRegistry(redis, prefix=s.registry_prefix)
    _warn_on_registry_divergence(qdrant, registry, app_logger)

    minio_client = Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
    )
    document_storage = DocumentStorage(minio_client, s.minio_bucket_documents)
    document_storage.ensure_bucket()
    user_document_storage = DocumentStorage(minio_client, s.minio_bucket_user_documents)
    user_document_storage.ensure_bucket()

    parser = DocumentParser(s)
    structure = StructureTagger(s)
    hierarchy = HierarchyBuilder()
    version_detector = VersionDetector()
    reference_extractor = ReferenceExtractor(s)
    reference_resolver = ReferenceResolver(qdrant, registry, s)

    # Kafka publishing (otteroad): events are queued in a Redis outbox and delivered
    # by the async publisher started in the lifespan. Without a configured broker the
    # publisher stays off and the pipeline skips enqueueing (outbox=None below).
    outbox = EventOutbox(redis, s)
    publisher = KafkaPublisher(outbox, s)

    ingestion = IngestionService(
        parser,
        structure,
        hierarchy,
        version_detector,
        reference_extractor,
        reference_resolver,
        qdrant,
        registry,
        document_storage,
        jobs,
        s,
        outbox=outbox if publisher.enabled else None,
    )
    search = SearchService(qdrant, s, user_index_registry)
    documents = DocumentsService(qdrant)
    editor = DocumentEditorService(qdrant, registry, s)
    library = LibraryService(qdrant, registry)
    tags = TagsService(qdrant)
    user_index_service = UserIndexService(
        qdrant,
        redis,
        user_index_registry,
        s,
        storage=user_document_storage,
        outbox=outbox if publisher.enabled else None,
    )

    system = SystemController(s)

    deps = Dependencies().set(
        settings=s,
        logger=app_logger,
        qdrant=qdrant,
        redis=redis,
        jobs=jobs,
        registry=registry,
        document_storage=document_storage,
        user_document_storage=user_document_storage,
        parser=parser,
        structure=structure,
        hierarchy=hierarchy,
        version_detector=version_detector,
        reference_extractor=reference_extractor,
        reference_resolver=reference_resolver,
        outbox=outbox,
        publisher=publisher,
        ingestion=ingestion,
        search=search,
        documents=documents,
        editor=editor,
        library=library,
        tags=tags,
        user_index_registry=user_index_registry,
        user_index_service=user_index_service,
        system=system,
    )
    log.info("dependencies_initialized", dependencies=repr(deps))
    return deps
