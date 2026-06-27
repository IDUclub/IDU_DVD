"""Qdrant point payload — the schema of a single stored node (text fragment or table)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.dvd_service.dto.reference import DocumentRef


class NodePayload(BaseModel):
    doc_id: str
    name: str  # document name/designation (filterable)
    version: str
    other_versions: list[str] = Field(
        default_factory=list
    )  # other versions of this document in the store
    content_hash: str  # full document-text hash (deduplication)
    source: str | None = None

    kind: str = "text"  # text | table — tables are separate entities
    type: str  # structural element type
    numbering: str = ""
    block: str = "main"  # main | amendment
    depth: int = 0

    parent_id: str | None = None
    parent_text: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    prev_id: str | None = None  # previous fragment in reading order
    next_id: str | None = None  # next fragment in reading order
    breadcrumb: str = ""

    tags: list[str] = Field(default_factory=list)
    table_html: str | None = None  # table structure (for kind=table)

    references: list[DocumentRef] = Field(
        default_factory=list
    )  # outgoing links to other documents/clauses

    text: str
