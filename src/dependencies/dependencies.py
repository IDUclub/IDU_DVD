"""Dependency declaration: the Dependencies singleton container and getters for endpoints.

Building and wiring the modules themselves is delegated to ``init_dependencies`` — this module
holds only the container, access to it, and getters for individual dependencies (for ``Depends``
in routers and MCP tools).
"""

from __future__ import annotations

from typing import Any

from src.broker.outbox import EventOutbox
from src.broker.publisher import KafkaPublisher
from src.common.config import Settings
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
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
from src.system_service.controllers import SystemController


class Dependencies:
    """Singleton container for all application dependencies.

    The values are set once at application startup (see ``init_dependencies``) and are then
    available from anywhere: HTTP routers, the MCP server, background tasks. Re-creating the
    object returns the same instance, so the getters always return the values recorded at
    startup.
    """

    # Field order = initialization order; used by as_dict/__repr__.
    _FIELDS: tuple[str, ...] = (
        "settings",
        "logger",
        "qdrant",
        "redis",
        "jobs",
        "registry",
        "parser",
        "structure",
        "hierarchy",
        "version_detector",
        "reference_extractor",
        "reference_resolver",
        "outbox",
        "publisher",
        "ingestion",
        "search",
        "documents",
        "editor",
        "library",
        "tags",
        "system",
    )

    _instance: "Dependencies | None" = None

    settings: Settings
    logger: Any
    qdrant: QdrantRepository
    redis: RedisClient
    jobs: JobStore
    registry: DocumentRegistry
    parser: DocumentParser
    structure: StructureTagger
    hierarchy: HierarchyBuilder
    version_detector: VersionDetector
    reference_extractor: ReferenceExtractor
    reference_resolver: ReferenceResolver
    outbox: EventOutbox
    publisher: KafkaPublisher
    ingestion: IngestionService
    search: SearchService
    documents: DocumentsService
    editor: DocumentEditorService
    library: LibraryService
    tags: TagsService
    system: SystemController

    def __new__(cls) -> "Dependencies":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # --- population at startup ---
    def set(
        self,
        *,
        settings: Settings,
        logger: Any,
        qdrant: QdrantRepository,
        redis: RedisClient,
        jobs: JobStore,
        registry: DocumentRegistry,
        parser: DocumentParser,
        structure: StructureTagger,
        hierarchy: HierarchyBuilder,
        version_detector: VersionDetector,
        reference_extractor: ReferenceExtractor,
        reference_resolver: ReferenceResolver,
        outbox: EventOutbox,
        publisher: KafkaPublisher,
        ingestion: IngestionService,
        search: SearchService,
        documents: DocumentsService,
        editor: DocumentEditorService,
        library: LibraryService,
        tags: TagsService,
        system: SystemController,
    ) -> "Dependencies":
        """Set all dependencies once (called from ``init_dependencies``)."""
        self.settings = settings
        self.logger = logger
        self.qdrant = qdrant
        self.redis = redis
        self.jobs = jobs
        self.registry = registry
        self.parser = parser
        self.structure = structure
        self.hierarchy = hierarchy
        self.version_detector = version_detector
        self.reference_extractor = reference_extractor
        self.reference_resolver = reference_resolver
        self.outbox = outbox
        self.publisher = publisher
        self.ingestion = ingestion
        self.search = search
        self.documents = documents
        self.editor = editor
        self.library = library
        self.tags = tags
        self.system = system
        return self

    # --- singleton access ---
    @classmethod
    def instance(cls) -> "Dependencies":
        """Return the initialized container, or raise if init was never called."""
        if cls._instance is None or not hasattr(cls._instance, "settings"):
            raise RuntimeError(
                "init_dependencies() не вызван — приложение не инициализировано"
            )
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for tests and re-initialization)."""
        cls._instance = None

    # --- getters for individual dependencies (for FastAPI Depends and MCP tools) ---
    @classmethod
    def get_settings(cls) -> Settings:
        return cls.instance().settings

    @classmethod
    def get_logger(cls) -> Any:
        return cls.instance().logger

    @classmethod
    def get_system(cls) -> SystemController:
        return cls.instance().system

    @classmethod
    def get_qdrant(cls) -> QdrantRepository:
        return cls.instance().qdrant

    @classmethod
    def get_redis(cls) -> RedisClient:
        return cls.instance().redis

    @classmethod
    def get_jobs(cls) -> JobStore:
        return cls.instance().jobs

    @classmethod
    def get_registry(cls) -> DocumentRegistry:
        return cls.instance().registry

    @classmethod
    def get_parser(cls) -> DocumentParser:
        return cls.instance().parser

    @classmethod
    def get_structure(cls) -> StructureTagger:
        return cls.instance().structure

    @classmethod
    def get_hierarchy(cls) -> HierarchyBuilder:
        return cls.instance().hierarchy

    @classmethod
    def get_version_detector(cls) -> VersionDetector:
        return cls.instance().version_detector

    @classmethod
    def get_reference_extractor(cls) -> ReferenceExtractor:
        return cls.instance().reference_extractor

    @classmethod
    def get_reference_resolver(cls) -> ReferenceResolver:
        return cls.instance().reference_resolver

    @classmethod
    def get_outbox(cls) -> EventOutbox:
        return cls.instance().outbox

    @classmethod
    def get_publisher(cls) -> KafkaPublisher:
        return cls.instance().publisher

    @classmethod
    def get_ingestion(cls) -> IngestionService:
        return cls.instance().ingestion

    @classmethod
    def get_search(cls) -> SearchService:
        return cls.instance().search

    @classmethod
    def get_documents(cls) -> DocumentsService:
        return cls.instance().documents

    @classmethod
    def get_editor(cls) -> DocumentEditorService:
        return cls.instance().editor

    @classmethod
    def get_library(cls) -> LibraryService:
        return cls.instance().library

    @classmethod
    def get_tags(cls) -> TagsService:
        return cls.instance().tags

    # --- representations ---
    def as_dict(self) -> dict[str, Any]:
        """Dependencies as a ``{name: object}`` dict in initialization order."""
        return {name: getattr(self, name) for name in self._FIELDS}

    def __repr__(self) -> str:
        if not hasattr(self, "settings"):
            return f"{type(self).__name__}(uninitialized)"
        body = ", ".join(f"{name}={getattr(self, name)!r}" for name in self._FIELDS)
        return f"{type(self).__name__}({body})"


def get_dependencies() -> Dependencies:
    """FastAPI dependency: the whole container (for ``Depends`` in routers/MCP)."""
    return Dependencies.instance()
