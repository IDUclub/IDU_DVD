"""Dependency initialization: building and wiring all modules at application startup.

The container declaration and getters live in ``dependencies``; this module only builds the
objects and stores them in the ``Dependencies`` singleton.
"""

from __future__ import annotations

import structlog

from src.common.config import Settings, settings
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
from src.common.logger import configure_logging
from src.dependencies.dependencies import Dependencies
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import Tagger, VersionDetector
from src.dvd_service.services.dvd_service import (
    DocumentsService,
    IngestionService,
    LibraryService,
    SearchService,
    TagsService,
)
from src.system_service.controllers import SystemController

log = structlog.get_logger(__name__)


def init_dependencies(s: Settings = settings) -> Dependencies:
    """Initialize and wire all modules, then store them in the ``Dependencies`` singleton.

    Called once at application startup (lifespan in ``src/main.py``).
    """
    # Configure logging first so everything below (and every module's logger) is captured
    # by the file + console sinks.
    configure_logging(s)
    app_logger = structlog.get_logger("app")

    qdrant = QdrantRepository(s)
    qdrant.ensure_collection()
    if s.enable_reference_linking:
        qdrant.ensure_pattern_collection()
    redis = RedisClient(s)
    jobs = JobStore(redis)
    registry = DocumentRegistry(redis)

    parser = DocumentParser(s)
    structure = StructureTagger(s)
    hierarchy = HierarchyBuilder()
    tagger = Tagger(s)
    version_detector = VersionDetector()
    reference_extractor = ReferenceExtractor(s)
    reference_resolver = ReferenceResolver(qdrant, registry, s)

    ingestion = IngestionService(
        parser,
        structure,
        hierarchy,
        tagger,
        version_detector,
        reference_extractor,
        reference_resolver,
        qdrant,
        registry,
        jobs,
        s,
    )
    search = SearchService(qdrant, s)
    documents = DocumentsService(qdrant)
    library = LibraryService(qdrant, registry)
    tags = TagsService(qdrant)

    system = SystemController(s)

    deps = Dependencies().set(
        settings=s,
        logger=app_logger,
        qdrant=qdrant,
        redis=redis,
        jobs=jobs,
        registry=registry,
        parser=parser,
        structure=structure,
        hierarchy=hierarchy,
        tagger=tagger,
        version_detector=version_detector,
        reference_extractor=reference_extractor,
        reference_resolver=reference_resolver,
        ingestion=ingestion,
        search=search,
        documents=documents,
        library=library,
        tags=tags,
        system=system,
    )
    log.info("dependencies_initialized", dependencies=repr(deps))
    return deps
