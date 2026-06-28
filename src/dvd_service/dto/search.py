"""API DTOs for vector search requests and responses."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.dvd_service.dto.reference import DocumentRef


class SearchRequest(BaseModel):
    query: str
    name: str | None = None  # filter by document name
    version: str | None = None  # filter by version
    block: str | None = None  # filter by main/amendment
    types: list[str] | None = (
        None  # filter by structural level (chapter/clause/subclause/...)
    )
    tags: list[str] | None = None  # filter by tags (any of)
    limit: int = 10
    context_height: int = 0  # how many neighbour fragments to attach before/after


class SearchHit(BaseModel):
    id: str
    score: float
    doc_id: str
    name: str
    version: str
    other_versions: list[str] = Field(default_factory=list)
    kind: str
    type: str
    block: str = "main"
    numbering: str = ""
    breadcrumb: str = ""
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    references: list[DocumentRef] = Field(default_factory=list)
    text: str
    context: str | None = (
        None  # expanded text with neighbours (when context_height > 0)
    )
    table_html: str | None = None


class SearchResponse(BaseModel):
    count: int
    hits: list[SearchHit]
