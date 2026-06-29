"""Document endpoints: upload (background parse + dedup) and background-job status."""

from __future__ import annotations

import json
import os
import shutil
import uuid

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
from src.dvd_service.dto import DocumentListResponse, JobStatusDTO, UploadResponse
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.services.dvd_service import DocumentsService, IngestionService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["documents"])


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


def _run_ingest(
    ingestion: IngestionService, job_id, path, raw, content_hash, version, meta
) -> None:
    try:
        ingestion.ingest(
            path,
            raw,
            content_hash,
            version_override=version,
            job_id=job_id,
            **meta,
        )
    except Exception:  # noqa: BLE001 — error status is already set inside ingest
        log.warning("background_ingest_error", job_id=job_id)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@router.post("/documents", response_model=UploadResponse, status_code=202)
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    version: str | None = Form(None),
    doc_type: str | None = Form(None),
    corpus: str | None = Form(None),
    lang: str | None = Form(None),
    title: str | None = Form(None),
    source_uri: str | None = Form(None),
    effective_date: str | None = Form(None),
    external_ids: str | None = Form(None),  # JSON object: {code, doi, isbn, url, …}
    metadata: str | None = Form(None),  # JSON object: free-form domain attributes
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Upload a document. Exact text duplicate -> 400; otherwise parse + index in the background.

    Optional metadata (``doc_type``, ``corpus``, ``lang``, ``title``, ``source_uri``,
    ``external_ids``/``metadata`` as JSON objects) is stored on every node so consumer services
    can join, filter, and cite without re-parsing.
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(
            415,
            "Поддерживаются только: %s (получено '%s')"
            % (", ".join(settings.allowed_extensions), ext or "—"),
        )

    meta = {
        "doc_type": doc_type,
        "corpus": corpus,
        "lang": lang,
        "title": title,
        "source_uri": source_uri,
        "effective_date": effective_date,
        "external_ids": _parse_json_field("external_ids", external_ids),
        "metadata": _parse_json_field("metadata", metadata),
    }

    os.makedirs(settings.upload_dir, exist_ok=True)
    job_id = str(uuid.uuid4())
    path = os.path.join(settings.upload_dir, f"{job_id}_{file.filename}")
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Cheap duplicate check BEFORE the heavy LLM pass: extract text and compute its hash.
    try:
        raw = await run_in_threadpool(parser.extract_raw, path)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(422, f"Не удалось разобрать файл: {exc}")
    content_hash = parser.content_hash(raw)

    if registry.has_hash(content_hash):
        info = registry.hash_info(content_hash) or {}
        os.remove(path)
        raise HTTPException(
            400,
            detail="Документ уже загружен — текст полностью совпадает (имя: %s, версия: %s)"
            % (info.get("name"), info.get("version")),
        )

    jobs.set(job_id, {"job_id": job_id, "status": "queued", "filename": file.filename})
    background.add_task(
        _run_ingest, ingestion, job_id, path, raw, content_hash, version, meta
    )
    return UploadResponse(job_id=job_id, status="queued")


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


@router.get("/documents/{job_id}", response_model=JobStatusDTO)
async def job_status(job_id: str, jobs: JobStore = Depends(Dependencies.get_jobs)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return JobStatusDTO(**job)
