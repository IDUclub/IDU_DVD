"""Document endpoints: upload / delta update / full reload / delete and background-job status."""

from __future__ import annotations

import os
import uuid
from functools import partial
from pathlib import Path

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from minio.error import S3Error

from src.common.auth import require_admin, require_authenticated
from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    ActiveJobsResponse,
    AvailableDocumentListResponse,
    DeleteResponse,
    DocumentListResponse,
    JobStatusDTO,
    QueuedJobDTO,
    QueueStateResponse,
    UploadResponse,
)
from src.dvd_service.dto.upload import ReparseAllResponse, ReparseSkipped, ReparseTarget
from src.dvd_service.ingest_queue import IngestQueue
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.routers._upload_common import document_meta as _document_meta
from src.dvd_service.routers._upload_common import (
    download_response as _download_response,
)
from src.dvd_service.routers._upload_common import ingest_entry as _ingest_entry
from src.dvd_service.routers._upload_common import park_source as _park_source
from src.dvd_service.routers._upload_common import (
    pick_source_point as _pick_source_point,
)
from src.dvd_service.routers._upload_common import queued_job as _queued_job
from src.dvd_service.routers._upload_common import receive_file as _receive_file
from src.dvd_service.routers._upload_common import reject_duplicate as _reject_duplicate
from src.dvd_service.services.dvd_service import (
    DocumentsService,
    IngestionService,
    LibraryService,
)
from src.dvd_service.services.version_repair import VersionRepairService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["documents"])

# The shared corpus is readable by anyone Keycloak still vouches for; changing it — and
# watching the ingestion that changes it — needs the admin role, a service account, or the
# admin panel.
AUTHENTICATED = [Depends(require_authenticated)]
ADMIN_ONLY = [Depends(require_admin)]


@router.post(
    "/documents/reparse",
    response_model=ReparseAllResponse,
    status_code=202,
    dependencies=ADMIN_ONLY,
)
def reparse_all_documents(
    documents: DocumentsService = Depends(Dependencies.get_documents),
    qdrant: QdrantRepository = Depends(Dependencies.get_qdrant),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
    name: str | None = Query(
        None,
        min_length=1,
        description="Exact document name; omitted means all documents.",
    ),
    version: str | None = Query(
        None,
        min_length=1,
        description="Exact edition; omitted means all editions of the selected document.",
    ),
    dry_run: bool = Query(
        False,
        description="Validate original availability and report targets without enqueueing or writing jobs.",
    ),
):
    """Reparse every stored corpus edition from its original, retaining identity.

    Explicit name/version selectors restrict the operation; UI filters do not. Missing originals and documents already
    queued/processing are reported individually; other documents still proceed.
    """
    grouped: dict[str, list[str]] = {}
    for document in documents.list_documents().documents:
        if name is not None and document.name != name:
            continue
        if version is not None and document.version != version:
            continue
        grouped.setdefault(document.name, []).append(document.version)
    planned = []
    skipped = []
    job_ids = []
    queued_versions = 0
    for name, versions in grouped.items():
        points = qdrant.points_by_name(name)
        editions = []
        for version in versions:
            # Shared fragments may carry another edition's original. Never silently
            # substitute that file for a version whose own source is unavailable.
            origins = [
                p
                for p in points
                if p.get("version") == version and p.get("source_object_key")
            ]
            target = origins[0] if origins else {}
            key = target.get("source_object_key")
            reason = None
            if not key:
                reason = "Нет сохранённого исходника этой версии"
            else:
                try:
                    if not storage.exists(key):
                        reason = "Исходник отсутствует в хранилище"
                except (
                    Exception
                ):  # an unavailable source must not abort the whole batch
                    log.exception(
                        "reparse_source_check_failed", name=name, version=version
                    )
                    reason = "Не удалось проверить исходник в хранилище"
            if reason:
                skipped.append(
                    ReparseSkipped(name=name, version=version, reason=reason)
                )
                continue
            meta = {
                field: target[field]
                for field in (
                    "doc_type",
                    "corpus",
                    "lang",
                    "title",
                    "source_uri",
                    "external_ids",
                    "metadata",
                    "effective_date",
                )
                if target.get(field) is not None
            }
            if (
                target.get("territory_source") == "manual"
                and target.get("territory_id") is not None
            ):
                meta["territory_id"] = target["territory_id"]
            editions.append(
                {
                    "version": version,
                    "doc_id": target.get("doc_id") or str(uuid.uuid4()),
                    "source_object_key": key,
                    "content_hash": target.get("content_hash") or "",
                    "filename": target.get("source") or f"{name}{Path(key).suffix}",
                    "meta": meta,
                }
            )
        if not editions:
            continue
        planned.extend(
            ReparseTarget(name=name, version=e["version"], doc_id=e["doc_id"])
            for e in editions
        )
        if dry_run:
            continue
        job_id = str(uuid.uuid4())
        if not queue.enqueue_reparse(
            {
                "job_id": job_id,
                "operation": "reparse",
                "name": name,
                "editions": editions,
            }
        ):
            skipped.extend(
                ReparseSkipped(
                    name=name,
                    version=e["version"],
                    reason="Документ уже в очереди или обрабатывается",
                )
                for e in editions
            )
            continue
        jobs.set_if_absent(
            job_id,
            {
                **_queued_job(job_id, None, "reparse", name),
                "version_total": len(editions),
                "version_index": 0,
            },
        )
        job_ids.append(job_id)
        queued_versions += len(editions)
    return ReparseAllResponse(
        queued_documents=len(job_ids),
        queued_versions=queued_versions,
        job_ids=job_ids,
        skipped=skipped,
        dry_run=dry_run,
        planned=planned,
    )


@router.post("/documents/version-repair", dependencies=ADMIN_ONLY)
async def repair_document_versions(
    dry_run: bool = Query(
        True, description="report the planned relabels, write nothing"
    ),
    repair: VersionRepairService = Depends(Dependencies.get_version_repair),
):
    """Relabel editions stored by the old version heuristic («СП 2.4.3648-20» as «3648»).

    Runs once after every startup as well; this is the way to preview it (the default
    ``dry_run``) or to run it again. Qdrant, the version registry and the document summary
    change together; an edition whose new label already exists is reported as a conflict.
    NormGraph picks the new labels up on its next reconcile (``POST /sync/reconcile``).
    """
    return await run_in_threadpool(partial(repair.run, dry_run=dry_run))


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=202,
    dependencies=ADMIN_ONLY,
)
async def upload_document(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    version: str | None = Form(None),
    meta: dict = Depends(_document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Upload a document. Exact text duplicate -> 400; otherwise parse + index in the background.

    ``name``/``version`` set the document identity manually and take precedence over LLM
    detection; without ``version`` the trailing year (1900–2099) of the name is used when present
    (e.g. ``СП 2.13130.2020`` -> ``2020``). Other optional metadata (``doc_type``, ``corpus``,
    ``lang``, ``title``, ``source_uri``, ``external_ids``/``metadata`` as JSON objects) is
    stored on every node so consumer services can join, filter, and cite without re-parsing. The
    original file is saved to MinIO before indexing starts (fail-closed: a storage failure
    rejects the request outright — nothing is queued).

    Indexing itself happens in a worker, not in this request: the job goes onto the durable
    ingestion queue and outlives both the client connection and the process.
    """
    job_id = str(uuid.uuid4())
    path, _, content_hash = await _receive_file(file, settings, parser, job_id)
    _reject_duplicate(registry, ingestion.qdrant, content_hash, path)
    try:
        source_key = await _park_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, _queued_job(job_id, file.filename, "upload", name))
    queue.enqueue(
        _ingest_entry(
            job_id,
            "upload",
            content_hash=content_hash,
            source_object_key=source_key,
            filename=file.filename,
            name=name,
            version=version,
            meta=meta,
            # Fixed now so that a retry re-indexes under the same id — which is what makes
            # cleaning up after an interrupted attempt exact.
            doc_id=str(uuid.uuid4()),
        )
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.patch(
    "/documents/{name}",
    response_model=UploadResponse,
    status_code=202,
    dependencies=ADMIN_ONLY,
)
async def update_document(
    name: str,
    file: UploadFile = File(...),
    version: str | None = Form(None),
    meta: dict = Depends(_document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Delta update of a stored document under a new version.

    Unchanged fragments only receive the new version tag; changed/added fragments are indexed
    anew next to them. The version comes from ``version``, else from the trailing year (1900–2099)
    of the name, else from LLM detection. Exact text duplicate -> 400, unknown name -> 404. The
    original file is saved to MinIO before indexing starts (fail-closed).
    """
    if not registry.has_name(name):
        raise HTTPException(404, f"Документ не найден: {name}")
    job_id = str(uuid.uuid4())
    path, _, content_hash = await _receive_file(file, settings, parser, job_id)
    _reject_duplicate(registry, ingestion.qdrant, content_hash, path)
    try:
        source_key = await _park_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, _queued_job(job_id, file.filename, "update", name))
    queue.enqueue(
        _ingest_entry(
            job_id,
            "update",
            content_hash=content_hash,
            source_object_key=source_key,
            filename=file.filename,
            name=name,
            version=version,
            meta=meta,
        )
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.put(
    "/documents/{name}",
    response_model=UploadResponse,
    status_code=202,
    dependencies=ADMIN_ONLY,
)
async def reload_document(
    name: str,
    file: UploadFile = File(...),
    version: str | None = Form(None),
    meta: dict = Depends(_document_meta),
    settings: Settings = Depends(Dependencies.get_settings),
    parser: DocumentParser = Depends(Dependencies.get_parser),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
):
    """Full reload (create-or-replace): wipe every stored version, then ingest from scratch.

    No duplicate rejection — re-uploading the same file is a legitimate way to rebuild the index.
    The original file is saved to MinIO before indexing starts (fail-closed).
    """
    job_id = str(uuid.uuid4())
    path, _, content_hash = await _receive_file(file, settings, parser, job_id)
    try:
        source_key = await _park_source(storage, path, content_hash)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(502, f"Не удалось сохранить исходник в хранилище: {exc}")

    jobs.set(job_id, _queued_job(job_id, file.filename, "reload", name))
    queue.enqueue(
        _ingest_entry(
            job_id,
            "reload",
            content_hash=content_hash,
            source_object_key=source_key,
            filename=file.filename,
            name=name,
            version=version,
            meta=meta,
        )
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.delete(
    "/documents/{name}", response_model=DeleteResponse, dependencies=ADMIN_ONLY
)
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


@router.get(
    "/documents", response_model=DocumentListResponse, dependencies=AUTHENTICATED
)
async def list_documents(
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    tags: list[str] | None = Query(None),
    uploaded_from: str | None = None,
    uploaded_to: str | None = None,
    document_level: str | None = Query(
        None, description="federal | regional | municipal"
    ),
    territory_ids: list[int] | None = Query(
        None,
        description="Urban API territory ids; matches the territory or anything above it",
    ),
    tagging_status: str | None = Query(
        None, description="ok | pending (pending = awaiting automatic tagging)"
    ),
    documents: DocumentsService = Depends(Dependencies.get_documents),
):
    """Documents already in the store, aggregated by (name, version), with optional filters.

    ``uploaded_from``/``uploaded_to`` are ISO 8601 timestamps (e.g. ``2026-06-01``).
    ``territory_ids`` filters on the stored ancestor chain, so asking for a municipality also
    returns the regional and federal documents in force there.
    """
    return await run_in_threadpool(
        partial(
            documents.list_documents,
            name,
            version,
            block,
            tags,
            uploaded_from,
            uploaded_to,
            document_level=document_level,
            territory_ids=territory_ids,
            tagging_status=tagging_status,
        )
    )


@router.get(
    "/documents/available",
    response_model=AvailableDocumentListResponse,
    dependencies=AUTHENTICATED,
)
async def list_available_documents(
    territory_ids: list[int] | None = Query(
        None,
        description="Urban API territory ids; includes every document in force there",
    ),
    library: LibraryService = Depends(Dependencies.get_library),
):
    """Fully indexed shared-corpus documents, optionally filtered by applicability."""
    return await run_in_threadpool(
        partial(library.list_available_documents, territory_ids=territory_ids)
    )


@router.get(
    "/documents/jobs/active",
    response_model=ActiveJobsResponse,
    dependencies=ADMIN_ONLY,
)
async def active_jobs(
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
):
    """Live progress supplemented by durable work whose progress record expired."""
    pending, inflight = queue.pending(), queue.inflight()
    by_id = {job["job_id"]: job for job in jobs.active()}
    for position, entry in enumerate(pending + inflight, 1):
        job_id = entry["job_id"]
        job = jobs.get(job_id)
        # A worker may have finished since the queue snapshot, before acknowledging it.
        if job and job.get("status") not in {"queued", "processing"}:
            by_id.pop(job_id, None)
            continue
        waiting = position <= len(pending)
        fallback = {
            **_queued_job(
                job_id, entry.get("filename"), entry["operation"], entry.get("name")
            ),
            "created_at": entry.get("enqueued_at"),
            "status": "queued" if waiting else "processing",
            "stage": "queued" if waiting else "preparing",
        }
        if entry["operation"] == "reparse":
            fallback.update(version_total=len(entry["editions"]), version_index=0)
        by_id[job_id] = {
            **fallback,
            **(job or {}),
            "queue_position": position if waiting else None,
        }
    active = list(by_id.values())
    return ActiveJobsResponse(
        count=len(active), jobs=[JobStatusDTO(**job) for job in active]
    )


@router.get(
    "/documents/jobs/recent",
    response_model=ActiveJobsResponse,
    dependencies=ADMIN_ONLY,
)
async def recent_jobs(
    limit: int = Query(20, ge=1, le=100),
    jobs: JobStore = Depends(Dependencies.get_jobs),
):
    """Recent ingestion jobs of every status, used by the admin progress history."""
    recent = jobs.recent(limit)
    return ActiveJobsResponse(
        count=len(recent), jobs=[JobStatusDTO(**job) for job in recent]
    )


@router.get(
    "/documents/jobs/queue",
    response_model=QueueStateResponse,
    dependencies=ADMIN_ONLY,
)
async def queue_state(queue: IngestQueue = Depends(Dependencies.get_ingest_queue)):
    """The durable ingestion queue: what is waiting, what a worker holds, what gave up.

    Unlike ``/documents/jobs/active`` (progress supplemented with durable work),
    this is the persisted work list — it is exactly what the service would resume after a
    restart. ``jobs`` lists the pending entries in processing order, then the in-flight ones.
    """
    pending, inflight = queue.pending(), queue.inflight()
    return QueueStateResponse(
        pending=len(pending),
        inflight=len(inflight),
        dead=len(queue.dead()),
        jobs=[QueuedJobDTO(**entry) for entry in pending + inflight],
    )


@router.get(
    "/documents/jobs/dead",
    response_model=QueueStateResponse,
    dependencies=ADMIN_ONLY,
)
async def dead_jobs(queue: IngestQueue = Depends(Dependencies.get_ingest_queue)):
    """Jobs that exhausted their processing attempts and are no longer retried on their own.

    Their originals are still in MinIO, so requeueing one (``POST
    /documents/jobs/{job_id}/retry``) is enough — the file does not have to be uploaded again.
    """
    dead = queue.dead()
    return QueueStateResponse(
        pending=queue.size(),
        inflight=queue.inflight_size(),
        dead=len(dead),
        jobs=[QueuedJobDTO(**entry) for entry in dead],
    )


@router.post(
    "/documents/jobs/{job_id}/retry",
    response_model=UploadResponse,
    status_code=202,
    dependencies=ADMIN_ONLY,
)
async def retry_dead_job(
    job_id: str,
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
):
    """Put a dead-lettered job back on the queue with its attempt counter reset."""
    entry = queue.requeue_dead(job_id)
    if entry is None:
        raise HTTPException(404, f"Задача не найдена среди отложенных: {job_id}")
    jobs.update(job_id, status="queued", error=None)
    log.info("ingest_job_requeued", job_id=job_id, operation=entry.get("operation"))
    return UploadResponse(job_id=job_id, status="queued")


@router.get("/documents/{job_id}", response_model=JobStatusDTO, dependencies=ADMIN_ONLY)
async def job_status(job_id: str, jobs: JobStore = Depends(Dependencies.get_jobs)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return JobStatusDTO(**job)


@router.post(
    "/documents/{name}/reindex",
    response_model=UploadResponse,
    status_code=202,
    dependencies=ADMIN_ONLY,
)
async def reindex_document(
    name: str,
    version: str | None = Query(
        None,
        description="Версия, чей исходник переиспользовать; без параметра — последняя",
    ),
    mode: str = Query(
        "replace",
        description=(
            "replace — перезалить под тем же именем; "
            "new — проиндексировать как новый документ, определив имя и версию заново"
        ),
    ),
    qdrant: QdrantRepository = Depends(Dependencies.get_qdrant),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
):
    """Re-run the pipeline over a document already in the store, without re-uploading it.

    The original never left the server: it is in MinIO, and its key is on every fragment of
    the document. So a re-run needs no file body — the job is queued straight from the stored
    key, and the worker downloads it exactly as it would a fresh upload. Useful after a model
    or parser change, and after an outage that indexed documents badly.

    ``mode=replace`` re-ingests under the same name (wipes the stored versions first, like
    ``PUT``). ``mode=new`` ingests it as a fresh document and lets the pipeline re-detect its
    identity — the repair path for documents whose name was resolved wrongly, where keeping
    the old name is the whole problem.

    **Order matters**: deleting a document also deletes its originals from MinIO, so a job
    queued against it must finish *before* the old document is deleted, not after.
    """
    if mode not in {"replace", "new"}:
        raise HTTPException(422, f"mode должен быть replace или new, получено '{mode}'")
    points = await run_in_threadpool(qdrant.points_by_name, name)
    target = _pick_source_point(points, version)
    key = target.get("source_object_key")
    if not key:
        raise HTTPException(
            404,
            "у документа нет сохранённого исходника — он загружен до появления MinIO-хранилища",
        )
    if not await run_in_threadpool(storage.exists, key):
        raise HTTPException(
            404,
            f"исходник отсутствует в хранилище (ключ {key}) — переиндексация невозможна",
        )

    job_id = str(uuid.uuid4())
    filename = target.get("source") or f"{name}{Path(key).suffix}"
    operation = "reload" if mode == "replace" else "upload"
    jobs.set(job_id, _queued_job(job_id, filename, operation, name))
    queue.enqueue(
        _ingest_entry(
            job_id,
            operation,
            content_hash=target.get("content_hash") or "",
            source_object_key=key,
            filename=filename,
            # mode=new deliberately passes no name: the identity is what has to be redone.
            name=name if mode == "replace" else None,
            doc_id=str(uuid.uuid4()) if mode == "new" else None,
        )
    )
    log.info(
        "document_reindex_queued",
        job_id=job_id,
        name=name,
        version=target.get("version"),
        mode=mode,
        source_object_key=key,
    )
    return UploadResponse(job_id=job_id, status="queued")


@router.get("/documents/{name}/source", dependencies=AUTHENTICATED)
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
