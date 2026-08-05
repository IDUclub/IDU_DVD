"""Direct document endpoints: caller-supplied fragments straight into Qdrant.

Unlike ``/documents`` (which runs the full LLM structuring pipeline over an uploaded ``.docx``),
these endpoints take already-split fragments as JSON, embed them, and upsert one point per
fragment. The resulting documents are first-class — same collection, same payload schema,
registered in the registry — so search / ``GET /documents`` / ``/library`` and
``DELETE /documents/{name}`` work on them unchanged.

Both endpoints take a JSON array of documents (a single document is an array of one) and queue
one background job per document, returning a per-document result list.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends

from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dependencies import Dependencies
from src.dvd_service.dto import DirectDocumentIn, DirectJobResult
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.routers._upload_common import duplicate_conflict, queued_job
from src.dvd_service.services.dvd_service import IngestionService

log = structlog.get_logger(__name__)
router = APIRouter(tags=["documents"])


def _content_hash(doc: DirectDocumentIn) -> str:
    """Dedup hash over the concatenated fragment texts (same normalization as the pipeline)."""
    return DocumentParser.content_hash([{"text": f.text} for f in doc.fragments])


def _run_direct_job(job_id: str, task) -> None:
    """Background wrapper: the service call maintains job status itself (incl. errors)."""
    try:
        task()
    except Exception:  # noqa: BLE001 — error status is already set by the service
        log.warning("direct_background_job_error", job_id=job_id)


@router.post("/documents/direct", response_model=list[DirectJobResult], status_code=202)
async def upload_documents_direct(
    docs: list[DirectDocumentIn],
    background: BackgroundTasks,
    registry: DocumentRegistry = Depends(Dependencies.get_registry),
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Directly ingest one or more documents from caller-supplied fragments.

    Each document is embedded and indexed in the background (no LLM structuring). An exact
    content duplicate is rejected per-document (``status="rejected"``); the rest are queued
    (``status="queued"`` + ``job_id``). Poll ``GET /documents/{job_id}`` for progress.
    """
    results: list[DirectJobResult] = []
    for doc in docs:
        content_hash = _content_hash(doc)
        conflict = duplicate_conflict(registry, ingestion.qdrant, content_hash)
        if conflict:
            results.append(
                DirectJobResult(name=doc.name, status="rejected", error=conflict)
            )
            continue
        job_id = str(uuid.uuid4())
        jobs.set(job_id, queued_job(job_id, None, "upload-direct", doc.name))
        background.add_task(
            _run_direct_job,
            job_id,
            lambda d=doc, ch=content_hash, jid=job_id: ingestion.ingest_direct(
                d, ch, job_id=jid
            ),
        )
        results.append(DirectJobResult(name=doc.name, status="queued", job_id=job_id))
    return results


@router.put("/documents/direct", response_model=list[DirectJobResult], status_code=202)
async def replace_documents_direct(
    docs: list[DirectDocumentIn],
    background: BackgroundTasks,
    jobs: JobStore = Depends(Dependencies.get_jobs),
    ingestion: IngestionService = Depends(Dependencies.get_ingestion),
):
    """Full replace (create-or-replace) of one or more directly-ingested documents by name.

    Every stored version of each named document is wiped, then the supplied fragments are
    ingested from scratch. No duplicate rejection — re-supplying the same fragments is a
    legitimate way to rebuild the index. Queues one background job per document.
    """
    results: list[DirectJobResult] = []
    for doc in docs:
        content_hash = _content_hash(doc)
        job_id = str(uuid.uuid4())
        jobs.set(job_id, queued_job(job_id, None, "reload-direct", doc.name))
        background.add_task(
            _run_direct_job,
            job_id,
            lambda d=doc, ch=content_hash, jid=job_id: ingestion.reload_direct(
                d, ch, job_id=jid
            ),
        )
        results.append(DirectJobResult(name=doc.name, status="queued", job_id=job_id))
    return results
