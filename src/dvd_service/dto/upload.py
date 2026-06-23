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
    doc_id: str | None = None
    name: str | None = None
    version: str | None = None
    other_versions: list[str] | None = None
    nodes: int | None = None
    error: str | None = None
