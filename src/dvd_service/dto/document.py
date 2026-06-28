"""API DTOs for the document listing endpoint — aggregated, per-document metadata."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentInfo(BaseModel):
    doc_id: str
    name: str
    version: str
    other_versions: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(
        default_factory=list
    )  # distinct block values present (main / amendment)
    tags: list[str] = Field(default_factory=list)  # union of fragment tags
    node_count: int = 0
    uploaded_at: str | None = None
    source: str | None = None


class DocumentListResponse(BaseModel):
    count: int
    documents: list[DocumentInfo]
