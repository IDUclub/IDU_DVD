"""API DTOs for vector search requests and responses."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.dvd_service.dto.reference import DocumentRef
from src.dvd_service.dto.scope import AdministrativeScope


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
    parent_id: str | None = (
        None  # search only among the direct children of this node (drill into a
        # table or a clause instead of the whole document)
    )
    doc_type: str | None = None  # filter by document type (regulation/article/…)
    corpus: str | None = None  # filter by logical corpus/namespace
    lang: str | None = None  # filter by language
    tags: list[str] | None = None  # filter by tags (any of)

    # --- administrative scope (Urban API territory tree) ---
    document_level: str | None = None  # federal | regional | municipal
    territory_ids: list[int] | None = (
        None  # match the territory or anything above it: the condition runs against the
        # stored ancestor chain, so asking for Vyborg also returns the regional and
        # federal documents that are in force there
    )
    tagging_status: str | None = None  # ok | pending (documents awaiting the backfill)
    limit: int = 10
    context_height: int = 0  # how many neighbour fragments to attach before/after

    # --- user-scoped index search (project_id or scenario_id; owner comes from the token) ---
    user_id: str | None = (
        None  # owner of the index; endpoints overwrite it with the authenticated caller
    )
    project_id: str | None = None  # canonical user-document isolation boundary
    scenario_id: str | None = (
        None  # compatibility lookup: Urban API resolves it to project_id
    )
    include_shared: bool = (
        True  # also match the shared/regular document corpus (combined search)
    )
    include_inherited: bool = (
        True  # deprecated compatibility flag; projects contain all scenario documents
    )

    @model_validator(mode="after")
    def _user_scope_requires_target(self) -> "SearchRequest":
        """An index needs a target; its owner does not have to be spelled out.

        ``user_id`` is resolved from the caller's token by the search endpoints, so a request
        may name only ``project_id``/``scenario_id`` — but naming an owner without a target
        still says nothing about which index to read.
        """
        if self.user_id and not (self.project_id or self.scenario_id):
            raise ValueError("user_id needs one of project_id or scenario_id")
        return self


class SearchHit(AdministrativeScope):
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


class TerritoryScope(BaseModel):
    """One territory that documents in the corpus are actually tagged with."""

    territory_id: int
    territory_name: str | None = None
    territory_type_name: str | None = None
    document_level: str | None = None
    document_count: int = 0


class ScopesResponse(BaseModel):
    """The administrative scopes present in the corpus — what it is worth filtering by.

    Deliberately *not* the Urban API catalogue: a territory with no documents would only give
    a caller an id that returns nothing.
    """

    levels: list[str] = Field(default_factory=list)
    territories: list[TerritoryScope] = Field(default_factory=list)
    pending_documents: int = 0  # documents still awaiting automatic tagging
