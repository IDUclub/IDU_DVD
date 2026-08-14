"""Administrative-scope tagging: inspect what is still pending and backfill it on demand."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.concurrency import run_in_threadpool

from src.dependencies import Dependencies
from src.dvd_service.services.tagging_backfill import TaggingBackfillService

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/tagging", tags=["tagging"])


@router.get("/pending")
async def pending_documents(
    limit: int | None = Query(None, ge=1),
    backfill: TaggingBackfillService = Depends(Dependencies.get_tagging_backfill),
):
    """Documents whose level/territory is still unresolved, with the reason for each.

    This is the admin panel's "needs attention" list: an Urban API outage, an ambiguous
    territory name, or a document whose head says nothing about who issued it.
    """
    documents = await run_in_threadpool(backfill.pending_documents, limit)
    return {
        "count": len(documents),
        "documents": [
            {
                "doc_id": doc["doc_id"],
                "name": doc["name"],
                "territory_id": doc["payload"].get("territory_id"),
                "tagging_error": doc["payload"].get("tagging_error"),
                "tagging_attempts": doc["payload"].get("tagging_attempts") or 0,
            }
            for doc in documents
        ],
    }


@router.post("/backfill", status_code=202)
async def run_backfill(
    background: BackgroundTasks,
    dry_run: bool = Query(False, description="resolve and report, write nothing"),
    limit: int | None = Query(None, ge=1),
    backfill: TaggingBackfillService = Depends(Dependencies.get_tagging_backfill),
):
    """Tag every pending document in the background; poll ``GET /documents/{job_id}``.

    Idempotent and safe to press twice: a sweep already in progress is not duplicated, a
    document that resolved is no longer pending, and a manually set territory is only
    completed, never reconsidered.
    """
    job_id = str(uuid.uuid4())
    background.add_task(
        lambda: backfill.run(dry_run=dry_run, limit=limit, job_id=job_id)
    )
    return {"job_id": job_id, "status": "queued", "dry_run": dry_run}
