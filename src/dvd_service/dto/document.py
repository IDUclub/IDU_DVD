"""API DTOs for document-level views.

Two complementary surfaces:
  * ``DocumentInfo`` / ``DocumentListResponse`` — the aggregated listing (per ``(name, version)``)
    computed from fragment payloads;
  * ``DocumentSummary`` / ``DocumentDetail`` / ``DocumentFragment`` / ``DocumentList`` — the
    MSI-TSIM-facing read API: enumerate documents and fetch one by ``doc_id`` as assembled text +
    metadata + ordered fragments (each with source grounding), instead of only semantic search.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.dvd_service.dto.reference import DocumentRef


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
    # populated only via the user-index listing path — the scenario a listed document actually
    # belongs to (own vs. inherited from a parent scenario)
    scenario_id: str | None = None
    # proxied download link (this service, not a raw MinIO URL) — None if no source was stored
    source_file_url: str | None = None


class DocumentListResponse(BaseModel):
    count: int
    documents: list[DocumentInfo]


class DocumentSummary(BaseModel):
    doc_id: str
    name: str
    title: str | None = None
    version: str
    version_id: str | None = None
    other_versions: list[str] = Field(default_factory=list)
    doc_type: str = "document"
    corpus: str = "default"
    lang: str | None = None
    status: str = "active"
    external_ids: dict = Field(default_factory=dict)
    source_uri: str | None = None
    content_hash: str | None = None
    node_count: int = 0
    uploaded_at: str | None = None
    effective_date: str | None = None
    metadata: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # proxied download link (this service, not a raw MinIO URL) — None if no source was stored
    source_file_url: str | None = None


class DocumentFragment(BaseModel):
    id: str
    order: int = 0
    kind: str = "text"
    type: str = ""
    numbering: str = ""
    depth: int = 0
    breadcrumb: str = ""
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    char_start: int | None = None
    char_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    span_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    # Outgoing links to other documents/clauses (from the node payload), so a consumer can
    # rebuild the cross-document reference graph from a single read instead of via search.
    references: list[DocumentRef] = Field(default_factory=list)
    text: str = ""
    table_html: str | None = None


class DocumentDetail(DocumentSummary):
    text: str = ""  # full document text assembled in reading order
    fragments: list[DocumentFragment] = Field(default_factory=list)


class DocumentList(BaseModel):
    count: int
    documents: list[DocumentSummary]


class DocumentUpdateRequest(BaseModel):
    """Editable document-wide payload fields; omitted fields stay unchanged."""

    title: str | None = None
    doc_type: str | None = None
    corpus: str | None = None
    lang: str | None = None
    status: str | None = None
    effective_date: str | None = None
    external_ids: dict | None = None
    metadata: dict | None = None
    tags: list[str] | None = None


class DocumentUpdateResponse(BaseModel):
    doc_id: str
    points_updated: int
    fields_updated: list[str]


class FragmentUpdateRequest(BaseModel):
    """Editable fragment fields; text changes always trigger re-embedding."""

    text: str | None = None
    tags: list[str] | None = None
    metadata: dict | None = None
    table_html: str | None = None
