"""Direct document endpoints: caller-supplied fragments straight into Qdrant.

Unlike ``/documents`` (which runs the full LLM structuring pipeline over an uploaded ``.docx``),
these endpoints take already-split fragments as JSON, embed them, and upsert one point per
fragment. The resulting documents are first-class — same collection, same payload schema,
registered in the registry — so search / ``GET /documents`` / ``/library`` and
``DELETE /documents/{name}`` work on them unchanged.

Both endpoints take a JSON array of documents (a single document is an array of one) and queue
one job per document on the durable ingestion queue, returning a per-document result list. The
fragments themselves are parked in MinIO first: unlike an upload there is no file to fall back
on, and a queued job must be replayable by a process that never saw the request.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException

from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dependencies import Dependencies
from src.dvd_service.dto import DirectDocumentIn, DirectJobResult
from src.dvd_service.ingest_queue import IngestQueue
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.routers._upload_common import (
    duplicate_conflict,
    ingest_entry,
    park_payload,
    queued_job,
)
from src.dvd_service.services.dvd_service import IngestionService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["documents"])


def _content_hash(doc: DirectDocumentIn) -> str:
    """Dedup hash over the concatenated fragment texts (same normalization as the pipeline)."""
    return DocumentParser.content_hash([{"text": f.text} for f in doc.fragments])


async def _queue_direct(
    doc: DirectDocumentIn,
    operation: str,
    *,
    storage: DocumentStorage,
    settings: Settings,
    jobs: JobStore,
    queue: IngestQueue,
) -> DirectJobResult:
    """Park one document's fragments in MinIO and queue the job that will index them."""
    job_id = str(uuid.uuid4())
    content_hash = _content_hash(doc)
    try:
        payload_key = await park_payload(
            storage, settings, job_id, doc.model_dump_json().encode("utf-8")
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, same rule as an upload
        raise HTTPException(502, f"Не удалось сохранить фрагменты в хранилище: {exc}")
    jobs.set(job_id, queued_job(job_id, None, operation, doc.name))
    queue.enqueue(
        ingest_entry(
            job_id,
            operation,
            content_hash=content_hash,
            payload_object_key=payload_key,
            name=doc.name,
            version=doc.version,
            doc_id=str(uuid.uuid4()) if operation == "upload-direct" else None,
        )
    )
    return DirectJobResult(name=doc.name, status="queued", job_id=job_id)


@router.post("/documents/direct", response_model=list[DirectJobResult], status_code=202)
async def upload_documents_direct(
    docs: list[DirectDocumentIn],
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    settings: Settings = Depends(Dependencies.get_settings),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Directly ingest one or more documents from caller-supplied fragments.

    Each document is embedded and indexed by a queue worker (no LLM structuring). An exact
    content duplicate is rejected per-document (``status="rejected"``); the rest are queued
    (``status="queued"`` + ``job_id``). Poll ``GET /documents/{job_id}`` for progress.
    """
    results: list[DirectJobResult] = []
    for doc in docs:
        conflict = duplicate_conflict(registry, ingestion.qdrant, _content_hash(doc))
        if conflict:
            results.append(
                DirectJobResult(name=doc.name, status="rejected", error=conflict)
            )
            continue
        results.append(
            await _queue_direct(
                doc,
                "upload-direct",
                storage=storage,
                settings=settings,
                jobs=jobs,
                queue=queue,
            )
        )
    return results


@router.put("/documents/direct", response_model=list[DirectJobResult], status_code=202)
async def replace_documents_direct(
    docs: list[DirectDocumentIn],
    settings: Settings = Depends(Dependencies.get_settings),
    storage: DocumentStorage = Depends(Dependencies.get_document_storage),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    queue: IngestQueue = Depends(Dependencies.get_ingest_queue),
):
    """Full replace (create-or-replace) of one or more directly-ingested documents by name.

    Every stored version of each named document is wiped, then the supplied fragments are
    ingested from scratch. No duplicate rejection — re-supplying the same fragments is a
    legitimate way to rebuild the index. Queues one job per document.
    """
    return [
        await _queue_direct(
            doc,
            "reload-direct",
            storage=storage,
            settings=settings,
            jobs=jobs,
            queue=queue,
        )
        for doc in docs
    ]
