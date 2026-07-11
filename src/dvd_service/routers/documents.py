"""Document endpoints: upload / delta update / full reload / delete and background-job status."""

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
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    ActiveJobsResponse,
    DeleteResponse,
    DocumentListResponse,
    JobStatusDTO,
    UploadResponse,
)
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.routers._upload_common import document_meta as _document_meta
from src.dvd_service.routers._upload_common import (
    download_response as _download_response,
)
from src.dvd_service.routers._upload_common import (
    pick_source_point as _pick_source_point,
)
from src.dvd_service.routers._upload_common import queued_job as _queued_job
from src.dvd_service.routers._upload_common import receive_file as _receive_file
from src.dvd_service.routers._upload_common import reject_duplicate as _reject_duplicate
from src.dvd_service.routers._upload_common import run_job as _run_job
from src.dvd_service.routers._upload_common import save_source as _save_source
from src.dvd_service.services.dvd_service import DocumentsService, IngestionService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["documents"])


@router.post("/documents", response_model=UploadResponse, status_code=202)
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    version: str | None = Form(None),
    meta: dict = Depends(_document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Upload a document. Exact text duplicate -> 400; otherwise parse + index in the background.

    ``name``/``version`` set the document identity manually and take precedence over LLM
    detection; without ``version`` the trailing 4-digit group of the name is used when present
    (e.g. ``СП 2.13130.2020`` -> ``2020``). Other optional metadata (``doc_type``, ``corpus``,
    ``lang``, ``title``, ``source_uri``, ``external_ids``/``metadata`` as JSON objects) is
    stored on every node so consumer services can join, filter, and cite without re-parsing. The
    original file is saved to MinIO before indexing starts (fail-closed: a storage failure
    rejects the request outright — nothing is queued).
    """
    job_id = str(uuid.uuid4())
    path, raw, content_hash = await _receive_file(file, settings, parser, job_id)
    _reject_duplicate(registry, content_hash, path)
    try:
        source_key = await _save_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, _queued_job(job_id, file.filename, "upload", name))
    background.add_task(
        _run_job,
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


@router.patch("/documents/{name}", response_model=UploadResponse, status_code=202)
async def update_document(
    name: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    version: str | None = Form(None),
    meta: dict = Depends(_document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Delta update of a stored document under a new version.

    Unchanged fragments only receive the new version tag; changed/added fragments are indexed
    anew next to them. The version comes from ``version``, else from the trailing 4-digit group
    of the name, else from LLM detection. Exact text duplicate -> 400, unknown name -> 404. The
    original file is saved to MinIO before indexing starts (fail-closed).
    """
    if not registry.has_name(name):
        raise HTTPException(404, f"Документ не найден: {name}")
    job_id = str(uuid.uuid4())
    path, raw, content_hash = await _receive_file(file, settings, parser, job_id)
    _reject_duplicate(registry, content_hash, path)
    try:
        source_key = await _save_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, _queued_job(job_id, file.filename, "update", name))
    background.add_task(
        _run_job,
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


@router.put("/documents/{name}", response_model=UploadResponse, status_code=202)
async def reload_document(
    name: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    version: str | None = Form(None),
    meta: dict = Depends(_document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Full reload (create-or-replace): wipe every stored version, then ingest from scratch.

    No duplicate rejection — re-uploading the same file is a legitimate way to rebuild the index.
    The original file is saved to MinIO before indexing starts (fail-closed).
    """
    job_id = str(uuid.uuid4())
    path, raw, content_hash = await _receive_file(file, settings, parser, job_id)
    try:
        source_key = await _save_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, _queued_job(job_id, file.filename, "reload", name))
    background.add_task(
        _run_job,
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


@router.delete("/documents/{name}", response_model=DeleteResponse)
async def delete_document(
    name: str,
    version: str | None = Query(
        None, description="Удалить только эту версию; без параметра — все версии"
    ),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Delete a document from the store — entirely, or a single version.

    Deleting one version removes its exclusive fragments and only strips the version tag from
    fragments shared with other versions.
    """
    try:
        result = await run_in_threadpool(ingestion.delete_document, name, version)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]) if exc.args else "не найдено")
    return DeleteResponse(**result)


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    tags: list[str] | None = Query(None),
    uploaded_from: str | None = None,
    uploaded_to: str | None = None,
    documents: DocumentsService = Depends(Dependencies.get_documents),
):
    """Documents already in the store, aggregated by (name, version), with optional filters.

    ``uploaded_from``/``uploaded_to`` are ISO 8601 timestamps (e.g. ``2026-06-01``).
    """
    return await run_in_threadpool(
        documents.list_documents, name, version, block, tags, uploaded_from, uploaded_to
    )


@router.get("/documents/jobs/active", response_model=ActiveJobsResponse)
async def active_jobs(jobs: JobStore = Depends(Dependencies.get_jobs)):
    """All queued and currently processing ingestion jobs."""
    active = jobs.active()
    return ActiveJobsResponse(
        count=len(active), jobs=[JobStatusDTO(**job) for job in active]
    )


@router.get("/documents/{job_id}", response_model=JobStatusDTO)
async def job_status(job_id: str, jobs: JobStore = Depends(Dependencies.get_jobs)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return JobStatusDTO(**job)


@router.get("/documents/{name}/source")
async def download_source(
    name: str,
    version: str | None = Query(
        None, description="Версия документа; без параметра — последняя"
    ),
    qdrant: QdrantRepository = Depends(Dependencies.get_qdrant),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
):
    """Proxy the original source file from MinIO — never a direct link to the closed contour."""
    points = await run_in_threadpool(qdrant.points_by_name, name)
    target = _pick_source_point(points, version)
    key = target.get("source_object_key")
    if not key:
        raise HTTPException(404, "исходный файл недоступен")
    try:
        data, content_type = await run_in_threadpool(storage.download, key)
    except S3Error:
        raise HTTPException(404, "исходный файл недоступен")
    filename = f"{name}{Path(key).suffix}"
    return _download_response(data, content_type, filename)
