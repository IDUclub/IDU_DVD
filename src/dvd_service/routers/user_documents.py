"""User-scoped document index endpoints: index lifecycle + document upload/update/reload/delete.

Mirrors ``/documents`` (see ``routers/documents.py``) but scoped to a ``(user_id, scenario_id)``
index, with a mandatory ``project_id`` tag and optional live inheritance from a
``parent_scenario_id``. No auth layer — ``user_id``/``project_id``/``scenario_id`` are explicit
caller-supplied params, matching the MCP server's existing unauthenticated model.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from minio.error import S3Error

from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.qdrant_client import QdrantRepository, user_scope_conditions
from src.common.db.redis_client import (
    DocumentRegistry,
    JobStore,
    RedisClient,
    UserIndexRegistry,
)
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DeleteResponse,
    DocumentListResponse,
    UploadResponse,
    UserIndexCreateRequest,
    UserIndexDeleteResponse,
    UserIndexInfo,
    UserIndexListResponse,
)
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.routers._upload_common import (
    document_meta,
    download_response,
    pick_source_point,
    queued_job,
    receive_file,
    reject_duplicate,
    run_job,
    save_source,
)
from src.dvd_service.services.dvd_service import DocumentsService
from src.dvd_service.services.user_index_service import (
    UserIndexService,
    build_user_ingestion_from_deps,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/user-documents", tags=["user-documents"])


def _build_ingestion(user_id: str, project_id: str, scenario_id: str):
    return build_user_ingestion_from_deps(
        Dependencies.instance(),
        user_id=user_id,
        project_id=project_id,
        scenario_id=scenario_id,
    )


def _scoped_registry(
    redis: RedisClient, settings: Settings, user_id: str, scenario_id: str
) -> DocumentRegistry:
    return DocumentRegistry(
        redis, prefix=f"{settings.registry_prefix}:user:{user_id}:{scenario_id}"
    )


# --- index lifecycle ---


@router.post("/index", response_model=UserIndexInfo, status_code=201)
async def create_index(
    body: UserIndexCreateRequest,
    service: UserIndexService = Depends(Dependencies.get_user_index_service),
):
    """Create a user document index — a ``(user_id, scenario_id)`` bucket, optionally inheriting
    (live, recursively) from another scenario's index."""
    try:
        return await run_in_threadpool(
            service.create_index,
            body.user_id,
            body.scenario_id,
            body.project_id,
            body.parent_scenario_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/index", response_model=UserIndexListResponse)
async def list_indices(
    user_id: str,
    service: UserIndexService = Depends(Dependencies.get_user_index_service),
):
    """All document indices belonging to a user."""
    return await run_in_threadpool(service.list_indices, user_id)


@router.delete("/index", response_model=UserIndexDeleteResponse)
async def delete_index(
    user_id: str,
    scenario_id: str,
    service: UserIndexService = Depends(Dependencies.get_user_index_service),
):
    """Wipe a user document index entirely (all its own documents; inherited ones are untouched
    since they belong to the parent scenario)."""
    try:
        return await run_in_threadpool(service.delete_index, user_id, scenario_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]) if exc.args else "не найдено")


# --- documents within an index ---


@router.post("", response_model=UploadResponse, status_code=202)
async def upload_user_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Form(...),
    scenario_id: str = Form(...),
    project_id: str = Form(...),
    parent_scenario_id: str | None = Form(
        None, description="Only honored the first time this index is created"
    ),
    name: str | None = Form(None),
    version: str | None = Form(None),
    meta: dict = Depends(document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    redis: RedisClient = Depends(Dependencies.get_redis),
    storage: DocumentStorage = Depends(Dependencies.get_user_document_storage),
    index_registry: UserIndexRegistry = Depends(Dependencies.get_user_index_registry),
    jobs: JobStore = Depends(Dependencies.get_jobs),
):
    """Upload a document into a user index (auto-created on first upload if it doesn't exist).

    The original file is saved to MinIO before indexing starts (fail-closed).
    """
    index_registry.get_or_create(user_id, scenario_id, project_id, parent_scenario_id)
    registry = _scoped_registry(redis, settings, user_id, scenario_id)
    ingestion = _build_ingestion(user_id, project_id, scenario_id)

    job_id = str(uuid.uuid4())
    path, raw, content_hash = await receive_file(file, settings, parser, job_id)
    reject_duplicate(registry, ingestion.qdrant, content_hash, path)
    try:
        source_key = await save_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, queued_job(job_id, file.filename, "upload", name))
    background.add_task(
        run_job,
        job_id,
        path,
        lambda: ingestion.ingest(
            path,
            raw,
            content_hash,
            version_override=version,
            job_id=job_id,
            name_override=name,
            source_object_key=source_key,
            **meta,
        ),
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.patch("/{name}", response_model=UploadResponse, status_code=202)
async def update_user_document(
    name: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Query(...),
    scenario_id: str = Query(...),
    version: str | None = Form(None),
    meta: dict = Depends(document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    redis: RedisClient = Depends(Dependencies.get_redis),
    storage: DocumentStorage = Depends(Dependencies.get_user_document_storage),
    index_registry: UserIndexRegistry = Depends(Dependencies.get_user_index_registry),
    jobs: JobStore = Depends(Dependencies.get_jobs),
):
    """Delta update of a document already in a user index.

    The original file is saved to MinIO before indexing starts (fail-closed).
    """
    record = index_registry.get(user_id, scenario_id)
    if record is None:
        raise HTTPException(404, f"индекс не найден: {user_id}/{scenario_id}")
    registry = _scoped_registry(redis, settings, user_id, scenario_id)
    if not registry.has_name(name):
        raise HTTPException(404, f"Документ не найден: {name}")
    ingestion = _build_ingestion(user_id, record["project_id"], scenario_id)

    job_id = str(uuid.uuid4())
    path, raw, content_hash = await receive_file(file, settings, parser, job_id)
    reject_duplicate(registry, ingestion.qdrant, content_hash, path)
    try:
        source_key = await save_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, queued_job(job_id, file.filename, "update", name))
    background.add_task(
        run_job,
        job_id,
        path,
        lambda: ingestion.update(
            name,
            path,
            raw,
            content_hash,
            version_override=version,
            job_id=job_id,
            source_object_key=source_key,
            **meta,
        ),
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.put("/{name}", response_model=UploadResponse, status_code=202)
async def reload_user_document(
    name: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Query(...),
    scenario_id: str = Query(...),
    version: str | None = Form(None),
    meta: dict = Depends(document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    storage: DocumentStorage = Depends(Dependencies.get_user_document_storage),
    index_registry: UserIndexRegistry = Depends(Dependencies.get_user_index_registry),
    jobs: JobStore = Depends(Dependencies.get_jobs),
):
    """Full reload (create-or-replace) of a document in a user index.

    The original file is saved to MinIO before indexing starts (fail-closed).
    """
    record = index_registry.get(user_id, scenario_id)
    if record is None:
        raise HTTPException(404, f"индекс не найден: {user_id}/{scenario_id}")
    ingestion = _build_ingestion(user_id, record["project_id"], scenario_id)

    job_id = str(uuid.uuid4())
    path, raw, content_hash = await receive_file(file, settings, parser, job_id)
    try:
        source_key = await save_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, queued_job(job_id, file.filename, "reload", name))
    background.add_task(
        run_job,
        job_id,
        path,
        lambda: ingestion.reload(
            name,
            path,
            raw,
            content_hash,
            version_override=version,
            job_id=job_id,
            source_object_key=source_key,
            **meta,
        ),
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.delete("/{name}", response_model=DeleteResponse)
async def delete_user_document(
    name: str,
    user_id: str = Query(...),
    scenario_id: str = Query(...),
    version: str | None = Query(
        None, description="Удалить только эту версию; без параметра — все версии"
    ),
    index_registry: UserIndexRegistry = Depends(Dependencies.get_user_index_registry),
):
    """Delete a document (or one of its versions) from a user index."""
    record = index_registry.get(user_id, scenario_id)
    if record is None:
        raise HTTPException(404, f"индекс не найден: {user_id}/{scenario_id}")
    ingestion = _build_ingestion(user_id, record["project_id"], scenario_id)
    try:
        result = await run_in_threadpool(ingestion.delete_document, name, version)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]) if exc.args else "не найдено")
    return DeleteResponse(**result)


@router.get("", response_model=DocumentListResponse)
async def list_user_documents(
    user_id: str,
    scenario_id: str,
    include_inherited: bool = True,
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    tags: list[str] | None = Query(None),
    uploaded_from: str | None = None,
    uploaded_to: str | None = None,
    qdrant: QdrantRepository = Depends(Dependencies.get_qdrant),
    index_registry: UserIndexRegistry = Depends(Dependencies.get_user_index_registry),
):
    """Documents in a user index, aggregated by (name, version) — includes the scenario's
    inheritance chain by default."""
    scenario_ids = (
        index_registry.ancestor_chain(user_id, scenario_id)
        if include_inherited
        else [scenario_id]
    )
    documents = DocumentsService(qdrant)
    return await run_in_threadpool(
        documents.list_documents,
        name,
        version,
        block,
        tags,
        uploaded_from,
        uploaded_to,
        user_id=user_id,
        scenario_ids=scenario_ids,
    )


@router.get("/{name}/source")
async def download_user_source(
    name: str,
    user_id: str = Query(...),
    scenario_id: str = Query(...),
    version: str | None = Query(
        None, description="Версия документа; без параметра — последняя"
    ),
    include_inherited: bool = True,
    qdrant: QdrantRepository = Depends(Dependencies.get_qdrant),
    storage: DocumentStorage = Depends(Dependencies.get_user_document_storage),
    index_registry: UserIndexRegistry = Depends(Dependencies.get_user_index_registry),
):
    """Proxy the original source file from MinIO, scoped to a user index (+ inheritance chain
    by default) — never a direct link to the closed contour."""
    scenario_ids = (
        index_registry.ancestor_chain(user_id, scenario_id)
        if include_inherited
        else [scenario_id]
    )
    extra_must = user_scope_conditions(user_id, scenario_ids)
    points = await run_in_threadpool(qdrant.points_by_name, name, extra_must)
    target = pick_source_point(points, version)
    key = target.get("source_object_key")
    if not key:
        raise HTTPException(404, "исходный файл недоступен")
    try:
        data, content_type = await run_in_threadpool(storage.download, key)
    except S3Error:
        raise HTTPException(404, "исходный файл недоступен")
    filename = f"{name}{Path(key).suffix}"
    return download_response(data, content_type, filename)
