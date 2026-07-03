"""Qdrant point payload — the schema of a single stored node (text fragment or table).

The payload carries several general-purpose (domain-neutral) layers:
  1. document identity (stable ids, codes, lookup keys) for cross-service joins;
  2. source grounding (offsets/page/bbox/span_id) so any consumer can cite the source;
  3. structure, references and open metadata/provenance so domain services (e.g. MSI-TSIM) build
     their own derived layers without DVD knowing the domain.

Every field beyond the original core has a safe default, so older points and minimal callers
keep validating. ``embedding_meta`` records which vectorizer produced the stored vector — a
forward hook for multi-vector / multi-search later.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.dvd_service.dto.reference import DocumentRef

PAYLOAD_SCHEMA_VERSION = 2


class NodePayload(BaseModel):
    # --- schema/provenance ---
    payload_schema_version: int = PAYLOAD_SCHEMA_VERSION
    uploaded_at: str = ""  # ISO 8601 UTC timestamp, set once per ingest call
    parser_version: str | None = None
    embedding_meta: dict = Field(
        default_factory=dict
    )  # {model, dim, metric, normalized} — vectorizer задел for multi-vector

    # --- document identity (general-purpose, domain-neutral) ---
    doc_id: str
    name: str  # document name/designation (filterable)
    title: str | None = None  # human-readable title, when distinct from name
    version: str  # version the fragment first appeared in
    versions: list[str] = Field(
        default_factory=list
    )  # every version this fragment belongs to (delta updates tag shared fragments)
    version_id: str | None = None  # stable id of this concrete revision/source file
    other_versions: list[str] = Field(
        default_factory=list
    )  # other versions of this document in the store
    content_hash: str  # full document-text hash (deduplication)

    doc_type: str = "document"  # document | regulation | article | book | web | …
    corpus: str = "default"  # logical corpus/namespace the document belongs to
    lang: str | None = None  # ISO-639 language code, when known

    external_ids: dict = Field(
        default_factory=dict
    )  # caller-supplied ids: {code, doi, isbn, url, …} — DVD stores, doesn't interpret
    aliases: list[str] = Field(default_factory=list)  # human-readable designations
    lookup_keys: list[str] = Field(
        default_factory=list
    )  # exact-match keys (normalized name + external id forms)

    # --- version lifecycle (general-purpose) ---
    status: str = "active"  # active | archived
    effective_date: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)

    # --- source / provenance ---
    source: str | None = None  # original filename (compat)
    source_uri: str | None = None  # file path / URL of the source

    # --- source grounding (path back to the source span) ---
    src_block_ids: list[int] = Field(
        default_factory=list
    )  # indices of the source raw blocks the node was built from (delta-update diffing)
    char_start: int | None = None  # offset into the normalized source text
    char_end: int | None = None
    page_start: int | None = None  # when the format exposes pages (PDF/scan)
    page_end: int | None = None
    bbox: list[float] | None = None  # [x0, y0, x1, y1] when available
    span_id: str | None = None

    # --- structure ---
    kind: str = "text"  # text | table — tables are separate entities
    type: str  # structural element type
    numbering: str = ""
    block: str = "main"  # main | amendment
    depth: int = 0
    order: int = 0  # position in document reading order (for reconstruction)

    parent_id: str | None = None
    parent_text: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    prev_id: str | None = None  # previous fragment in reading order
    next_id: str | None = None  # next fragment in reading order
    breadcrumb: str = ""

    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(
        default_factory=dict
    )  # open extension slot for domain-specific attributes
    table_html: str | None = None  # table structure (for kind=table)

    references: list[DocumentRef] = Field(
        default_factory=list
    )  # outgoing links to other documents/clauses

    text: str
