"""Document endpoints: upload / delta update / full reload / delete and background-job status."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone

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

from src.common.config import Settings
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
from src.dvd_service.services.dvd_service import DocumentsService, IngestionService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["documents"])


def _queued_job(
    job_id: str, filename: str | None, operation: str, name: str | None = None
) -> dict:
    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "operation": operation,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_json_field(name: str, value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"Поле '{name}' должно быть JSON-объектом: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(422, f"Поле '{name}' должно быть JSON-объектом")
    return data


async def _document_meta(
    doc_type: str | None = Form(None),
    corpus: str | None = Form(None),
    lang: str | None = Form(None),
    title: str | None = Form(None),
    source_uri: str | None = Form(None),
    effective_date: str | None = Form(None),
    external_ids: str | None = Form(None),  # JSON object: {code, doi, isbn, url, …}
    metadata: str | None = Form(None),  # JSON object: free-form domain attributes
) -> dict:
    """Optional per-document metadata form fields, shared by upload/update/reload."""
    return {
        "doc_type": doc_type,
        "corpus": corpus,
        "lang": lang,
        "title": title,
        "source_uri": source_uri,
        "effective_date": effective_date,
        "external_ids": _parse_json_field("external_ids", external_ids),
        "metadata": _parse_json_field("metadata", metadata),
    }


async def _receive_file(
    file: UploadFile, settings: Settings, parser: DocumentParser, job_id: str
) -> tuple[str, list[dict], str]:
    """Validate the extension, persist the upload and pre-parse it: (path, raw, hash)."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(
            415,
            "Поддерживаются только: %s (получено '%s')"
            % (", ".join(settings.allowed_extensions), ext or "—"),
        )
    os.makedirs(settings.upload_dir, exist_ok=True)
    path = os.path.join(settings.upload_dir, f"{job_id}_{file.filename}")
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # Cheap text extraction BEFORE the heavy LLM pass — also powers the duplicate check.
    try:
        raw = await run_in_threadpool(parser.extract_raw, path)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(422, f"Не удалось разобрать файл: {exc}")
    return path, raw, parser.content_hash(raw)


def _reject_duplicate(registry: DocumentRegistry, content_hash: str, path: str) -> None:
    if registry.has_hash(content_hash):
        info = registry.hash_info(content_hash) or {}
        os.remove(path)
        raise HTTPException(
            400,
            detail="Документ уже загружен — текст полностью совпадает (имя: %s, версия: %s)"
            % (info.get("name"), info.get("version")),
        )


def _run_job(job_id: str, path: str, task) -> None:
    """Background wrapper: job status is maintained inside the service call itself."""
    try:
        task()
    except Exception:  # noqa: BLE001 — error status is already set by the service
        log.warning("background_job_error", job_id=job_id)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Upload a document. Exact text duplicate -> 400; otherwise parse + index in the background.

    ``name``/``version`` set the document identity manually and take precedence over LLM
    detection; without ``version`` the trailing 4-digit group of the name is used when present
    (e.g. ``СП 2.13130.2020`` -> ``2020``). Other optional metadata (``doc_type``, ``corpus``,
    ``lang``, ``title``, ``source_uri``, ``external_ids``/``metadata`` as JSON objects) is
    stored on every node so consumer services can join, filter, and cite without re-parsing.
    """
    job_id = str(uuid.uuid4())
    path, raw, content_hash = await _receive_file(file, settings, parser, job_id)
    _reject_duplicate(registry, content_hash, path)

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
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Delta update of a stored document under a new version.

    Unchanged fragments only receive the new version tag; changed/added fragments are indexed
    anew next to them. The version comes from ``version``, else from the trailing 4-digit group
    of the name, else from LLM detection. Exact text duplicate -> 400, unknown name -> 404.
    """
    if not registry.has_name(name):
        raise HTTPException(404, f"Документ не найден: {name}")
    job_id = str(uuid.uuid4())
    path, raw, content_hash = await _receive_file(file, settings, parser, job_id)
    _reject_duplicate(registry, content_hash, path)

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
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Full reload (create-or-replace): wipe every stored version, then ingest from scratch.

    No duplicate rejection — re-uploading the same file is a legitimate way to rebuild the index.
    """
    job_id = str(uuid.uuid4())
    path, raw, content_hash = await _receive_file(file, settings, parser, job_id)

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
