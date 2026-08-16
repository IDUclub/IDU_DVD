"""User-scoped document indices: create/list/delete indices, and the per-request factory that
builds a scoped ``IngestionService`` for uploading/updating/deleting documents inside one.

Document data is keyed by ``(user_id, project_id)``.  Scenario-shaped index records remain as a
compatibility catalogue for older clients, but never define the Qdrant or document-registry scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from qdrant_client.models import Filter

from src.broker.events import DocumentDeleted
from src.broker.outbox import EventOutbox, ScopedEventOutbox
from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.qdrant_client import (
    QdrantRepository,
    ScopedQdrantRepository,
    user_scope_conditions,
)
from src.common.db.redis_client import (
    DocumentRegistry,
    JobStore,
    RedisClient,
    UserIndexRegistry,
)
from src.dvd_service.dto.user_index import (
    UserIndexDeleteResponse,
    UserIndexInfo,
    UserIndexListResponse,
)
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import VersionDetector
from src.dvd_service.services.dvd_service import IngestionService

if TYPE_CHECKING:
    from src.dependencies.dependencies import Dependencies

log = structlog.get_logger(__name__)


def _registry_prefix(settings: Settings, user_id: str, project_id: str) -> str:
    return f"{settings.registry_prefix}:user:{user_id}:project:{project_id}"


def _migrate_legacy_registry(
    redis: RedisClient,
    settings: Settings,
    user_id: str,
    scenario_id: str | None,
    project_id: str,
) -> None:
    """Merge one legacy scenario registry into the project registry, once.

    Qdrant points written by the previous model already carry ``project_id``. Redis metadata was
    the remaining scenario-scoped piece; copying it lazily keeps duplicate detection and delta
    updates working immediately after a rolling deployment without rewriting Qdrant.
    """
    if not scenario_id:
        return
    old_prefix = f"{settings.registry_prefix}:user:{user_id}:{scenario_id}"
    new_prefix = _registry_prefix(settings, user_id, project_id)
    marker = f"{new_prefix}:migrated_scenario:{scenario_id}"
    if redis.r.exists(marker):
        return
    for old_key in redis.r.scan_iter(match=f"{old_prefix}:*"):
        suffix = old_key[len(old_prefix) :]
        new_key = f"{new_prefix}{suffix}"
        key_type = redis.r.type(old_key)
        if key_type == "string":
            redis.r.setnx(new_key, redis.r.get(old_key))
        elif key_type == "set":
            values = redis.r.smembers(old_key)
            if values:
                redis.r.sadd(new_key, *values)
        elif key_type == "list":
            values = redis.r.lrange(old_key, 0, -1)
            if values and not redis.r.exists(new_key):
                redis.r.rpush(new_key, *values)
    redis.r.set(marker, "1")


def build_user_ingestion(
    *,
    settings: Settings,
    qdrant: QdrantRepository,
    redis: RedisClient,
    storage: DocumentStorage,
    jobs: JobStore,
    parser: DocumentParser,
    structure: StructureTagger,
    hierarchy: HierarchyBuilder,
    version_detector: VersionDetector,
    reference_extractor: ReferenceExtractor,
    reference_resolver: ReferenceResolver,
    outbox: EventOutbox | None,
    user_id: str,
    project_id: str,
    scenario_id: str | None = None,
) -> IngestionService:
    """Build a per-request ``IngestionService`` scoped to one user document index.

    Reuses the pipeline's shared, stateless modules (parser/structure/hierarchy/version_detector)
    and reuses ``IngestionService`` itself unmodified — scoping is entirely injected via a scoped
    ``DocumentRegistry`` (isolated dedup/version/name state), a ``ScopedQdrantRepository`` (stamps
    every point and restricts every lookup to this exact index), and a settings copy with
    reference-linking disabled (that stage resolves cross-document links against the shared,
    unscoped collection — not meaningful, and not safe, for ad hoc user uploads).
    """
    _migrate_legacy_registry(redis, settings, user_id, scenario_id, project_id)
    scoped_registry = DocumentRegistry(
        redis, prefix=_registry_prefix(settings, user_id, project_id)
    )
    scoped_qdrant = ScopedQdrantRepository(
        qdrant, user_id=user_id, project_id=project_id, scenario_id=scenario_id
    )
    scoped_settings = settings.model_copy(update={"enable_reference_linking": False})
    scoped_outbox = (
        ScopedEventOutbox(
            outbox,
            user_id=user_id,
            project_id=project_id,
            scenario_id=scenario_id,
        )
        if outbox is not None
        else None
    )
    return IngestionService(
        parser,
        structure,
        hierarchy,
        version_detector,
        reference_extractor,
        reference_resolver,
        scoped_qdrant,
        scoped_registry,
        storage,
        jobs,
        scoped_settings,
        outbox=scoped_outbox,
    )


def build_user_ingestion_from_deps(
    deps: "Dependencies",
    *,
    user_id: str,
    project_id: str,
    scenario_id: str | None = None,
) -> IngestionService:
    """Convenience wrapper around ``build_user_ingestion`` reading from the ``Dependencies``
    singleton — the single implementation shared by the REST router and the MCP tools.
    """
    return build_user_ingestion(
        settings=deps.settings,
        qdrant=deps.qdrant,
        redis=deps.redis,
        storage=deps.user_document_storage,
        jobs=deps.jobs,
        parser=deps.parser,
        structure=deps.structure,
        hierarchy=deps.hierarchy,
        version_detector=deps.version_detector,
        reference_extractor=deps.reference_extractor,
        reference_resolver=deps.reference_resolver,
        outbox=deps.outbox if deps.publisher.enabled else None,
        user_id=user_id,
        project_id=project_id,
        scenario_id=scenario_id,
    )


class UserIndexService:
    """Manage legacy scenario index records over project-scoped document storage.

    Create/list remain compatible with existing callers. Deleting any record removes the whole
    ``(user_id, project_id)`` document scope and every scenario record pointing to that project.
    """

    def __init__(
        self,
        qdrant: QdrantRepository,
        redis: RedisClient,
        index_registry: UserIndexRegistry,
        settings: Settings,
        *,
        storage: DocumentStorage,
        outbox: EventOutbox | None = None,
    ) -> None:
        self.qdrant = qdrant
        self.redis = redis
        self.index_registry = index_registry
        self.settings = settings
        self.storage = storage
        self.outbox = outbox

    def __repr__(self) -> str:
        return f"{type(self).__name__}(qdrant={type(self.qdrant).__name__})"

    @staticmethod
    def _to_info(record: dict, document_count: int) -> UserIndexInfo:
        return UserIndexInfo(**record, document_count=document_count)

    def _document_count(self, user_id: str, project_id: str) -> int:
        flt = Filter(must=user_scope_conditions(user_id, [project_id]))
        return self.qdrant.count(flt)

    def create_index(
        self,
        user_id: str,
        scenario_id: str,
        project_id: str,
        parent_scenario_id: str | None = None,
    ) -> UserIndexInfo:
        record = self.index_registry.create(
            user_id, scenario_id, project_id, parent_scenario_id
        )
        return self._to_info(record, document_count=0)

    def list_indices(self, user_id: str) -> UserIndexListResponse:
        records = self.index_registry.list_for_user(user_id)
        indices = [
            self._to_info(r, self._document_count(user_id, r["project_id"]))
            for r in records
        ]
        return UserIndexListResponse(count=len(indices), indices=indices)

    def delete_index(self, user_id: str, scenario_id: str) -> UserIndexDeleteResponse:
        """Wipe a user document index entirely.

        Announces a ``DocumentDeleted`` event (``document_removed=True``) per distinct document
        name found in the index, scoped via ``ScopedEventOutbox`` — otherwise a consumer built on
        top of ``document.events`` (e.g. NormGraph) has no way to learn a whole index was wiped in
        one call, since this bypasses ``IngestionService.delete_document``'s own per-document event.
        """
        record = self.index_registry.get(user_id, scenario_id)
        if record is None:
            raise KeyError(f"index not found: {user_id}/{scenario_id}")
        project_id = str(record["project_id"])
        flt = Filter(must=user_scope_conditions(user_id, [project_id]))
        payloads = self.qdrant.scroll_payloads(flt)
        source_keys = {
            pl["source_object_key"] for pl in payloads if pl.get("source_object_key")
        }
        versions_by_name: dict[str, set[str]] = {}
        for pl in payloads:
            name = pl.get("name")
            if not name:
                continue
            tags = versions_by_name.setdefault(name, set())
            if pl.get("version"):
                tags.add(pl["version"])
            tags.update(pl.get("versions") or [])
        points_deleted = len(payloads)
        self.qdrant.delete_by_filter(flt)
        for key in source_keys:
            self.storage.delete(key)
        DocumentRegistry(
            self.redis, prefix=_registry_prefix(self.settings, user_id, project_id)
        ).wipe()
        project_records = [
            item
            for item in self.index_registry.list_for_user(user_id)
            if str(item.get("project_id")) == project_id
        ]
        for item in project_records:
            legacy_scenario_id = item["scenario_id"]
            DocumentRegistry(
                self.redis,
                prefix=(
                    f"{self.settings.registry_prefix}:user:"
                    f"{user_id}:{legacy_scenario_id}"
                ),
            ).wipe()
            self.index_registry.delete(user_id, legacy_scenario_id)
        if self.outbox is not None:
            scoped_outbox = ScopedEventOutbox(
                self.outbox,
                user_id=user_id,
                project_id=project_id,
                scenario_id=scenario_id,
            )
            for name, versions in versions_by_name.items():
                scoped_outbox.enqueue(
                    DocumentDeleted(
                        document_name=name,
                        versions_removed=sorted(versions),
                        document_removed=True,
                    )
                )
        log.info(
            "user_index_deleted",
            user_id=user_id,
            scenario_id=scenario_id,
            points_deleted=points_deleted,
            documents_deleted=len(versions_by_name),
        )
        return UserIndexDeleteResponse(
            user_id=user_id, scenario_id=scenario_id, points_deleted=points_deleted
        )
