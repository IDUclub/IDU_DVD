"""API DTOs for vector search requests and responses."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.dvd_service.dto.reference import DocumentRef


class SearchRequest(BaseModel):
    query: str
    name: str | None = None  # filter by a single document name
    document_names: list[str] | None = None  # filter by any of these document names
    version: str | None = None  # filter by version
    block: str | None = None  # filter by main/amendment
    types: list[str] | None = (
        None  # filter by structural level (chapter/clause/subclause/...)
    )
    doc_id: str | None = None  # filter by a specific document
    doc_type: str | None = None  # filter by document type (regulation/article/…)
    corpus: str | None = None  # filter by logical corpus/namespace
    lang: str | None = None  # filter by language
    tags: list[str] | None = None  # filter by tags (any of)
    limit: int = 10
    context_height: int = 0  # how many neighbour fragments to attach before/after

    # --- user-scoped index search (both user_id and scenario_id, or neither) ---
    user_id: str | None = None  # owner of the user document index to search
    project_id: str | None = None  # filter tag only, not an isolation boundary
    scenario_id: str | None = None  # scenario whose index (+ inheritance chain) to search
    include_shared: bool = (
        True  # also match the shared/regular document corpus (combined search)
    )
    include_inherited: bool = (
        True  # also match the scenario's ancestor chain (live inheritance)
    )

    @model_validator(mode="after")
    def _user_scope_requires_both(self) -> "SearchRequest":
        if bool(self.user_id) != bool(self.scenario_id):
            raise ValueError("user_id and scenario_id must be given together")
        return self


class SearchHit(BaseModel):
    id: str
    score: float
    doc_id: str
    name: str
    title: str | None = None
    version: str
    versions: list[str] = Field(
        default_factory=list
    )  # all versions containing the fragment
    version_id: str | None = None
    other_versions: list[str] = Field(default_factory=list)

    doc_type: str = "document"
    corpus: str = "default"
    lang: str | None = None
    external_ids: dict = Field(default_factory=dict)

    user_id: str | None = None
    project_id: str | None = None
    scenario_id: str | None = None

    kind: str
    type: str
    block: str = "main"
    numbering: str = ""
    breadcrumb: str = ""
    depth: int = 0
    order: int = 0
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None

    # source grounding — lets the caller cite the exact source location
    source_uri: str | None = None
    # proxied download link (this service, not a raw MinIO URL) — None if no source was stored
    source_file_url: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    span_id: str | None = None

    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    references: list[DocumentRef] = Field(default_factory=list)
    text: str
    context: str | None = (
        None  # expanded text with neighbours (when context_height > 0)
    )
    table_html: str | None = None


class SearchResponse(BaseModel):
    count: int
    hits: list[SearchHit]


class TagsResponse(BaseModel):
    count: int
    tags: list[str]
