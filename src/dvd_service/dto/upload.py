"""API DTOs for document upload and background-job status."""

from __future__ import annotations

from pydantic import BaseModel


class UploadResponse(BaseModel):
    job_id: str
    status: str


class JobStatusDTO(BaseModel):
    job_id: str
    status: str  # queued | processing | done | error
    filename: str | None = None
    # --- live per-document progress (populated while status == "processing") ---
    stage: str | None = None  # human-readable current pipeline stage
    stage_index: int | None = None  # 1-based index of the current stage
    stage_total: int | None = None  # total number of stages in the pipeline
    phase: str | None = (
        None  # sub-phase within the stage (e.g. "boundaries", "semantic-merge pass 2")
    )
    progress: int | None = None  # LLM requests processed within the current stage/phase
    progress_total: int | None = (
        None  # total requests in the current stage/phase (None if not chunked)
    )
    task_progress: int | None = None  # current stage/phase, normalized to 0..100
    overall_progress: int | None = None  # file transfer + weighted pipeline, 0..100
    doc_id: str | None = None
    name: str | None = None
    version: str | None = None
    other_versions: list[str] | None = None
    nodes: int | None = None
    new_nodes: int | None = None  # delta update: fragments inserted for the new version
    reused_nodes: int | None = (
        None  # delta update: fragments shared with the base version
    )
    error: str | None = None
    operation: str | None = None  # upload | update | reload
    created_at: str | None = None


class ActiveJobsResponse(BaseModel):
    count: int
    jobs: list[JobStatusDTO]


class QueuedJobDTO(BaseModel):
    """One entry of the durable ingestion queue (what a worker needs to run the job).

    Distinct from :class:`JobStatusDTO`, which reports live pipeline progress: this is the
    persisted work item that survives a restart.
    """

    job_id: str
    operation: str  # upload | update | reload | upload-direct | reload-direct
    name: str | None = None
    filename: str | None = None
    attempts: int = 0
    enqueued_at: str | None = None
    failed_at: str | None = None
    last_error: str | None = None


class QueueStateResponse(BaseModel):
    """Depth of the ingestion queue and the jobs in it."""

    pending: int  # waiting for a worker
    inflight: int  # claimed by a worker, being processed right now
    dead: int  # exhausted their attempts; retried only on request
    jobs: list[QueuedJobDTO]


class DeleteResponse(BaseModel):
    name: str
    versions_removed: list[str]
    points_deleted: int  # fragments removed from the vector store
    points_updated: int  # shared fragments that only lost a version tag
