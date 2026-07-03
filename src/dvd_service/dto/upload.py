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
    new_nodes: int | None = None  # delta update: fragments inserted for the new version
    reused_nodes: int | None = (
        None  # delta update: fragments shared with the base version
    )
    error: str | None = None


class DeleteResponse(BaseModel):
    name: str
    versions_removed: list[str]
    points_deleted: int  # fragments removed from the vector store
    points_updated: int  # shared fragments that only lost a version tag
