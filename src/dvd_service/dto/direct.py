"""API DTOs for direct document ingestion — caller-supplied fragments straight into Qdrant.

The direct path bypasses the LLM structuring pipeline entirely: the caller sends already-split
fragments, DVD only embeds them and upserts the points. Documents stay first-class (same
collection, same ``NodePayload`` schema, registered in the registry) so search / ``/documents`` /
``/library`` and ``DELETE /documents/{name}`` work on them unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class DirectFragmentIn(BaseModel):
    """One caller-supplied fragment. Only ``text`` is required; the rest has sane defaults.

    Fragments are stored in array order: ``order`` is the position and ``prev_id``/``next_id`` are
    linked between neighbours so the search context assembler works out of the box.
    """

    text: str = Field(..., min_length=1)
    type: str = "paragraph"  # structural element type
    kind: str = "text"  # text | table
    numbering: str = ""
    block: str = "main"  # main | amendment
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(
        default_factory=dict
    )  # merged over the document-level metadata
    table_html: str | None = None  # table structure (for kind=table)
    parent_id: str | None = None  # optional caller-supplied hierarchy link

    @field_validator("text")
    @classmethod
    def _non_blank_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text не может быть пустым")
        return v


class DirectDocumentIn(BaseModel):
    """One document to ingest directly. Only ``name`` and a non-empty ``fragments`` are required."""

    name: str = Field(..., min_length=1)
    version: str | None = None  # else trailing 4-digit group of the name, else "1"
    doc_type: str | None = None
    corpus: str | None = None
    lang: str | None = None
    title: str | None = None
    source_uri: str | None = None
    effective_date: str | None = None
    external_ids: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    embedding_provider: str | None = (
        None  # reserved: must match the configured provider for now
    )
    fragments: list[DirectFragmentIn] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def _non_blank_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name не может быть пустым")
        return v


class DirectJobResult(BaseModel):
    """Per-document outcome of a direct upload/replace call.

    Valid documents are queued (``status="queued"`` + ``job_id``); a document rejected up front
    (e.g. an exact-content duplicate on create) carries ``status="rejected"`` + ``error`` and no
    ``job_id``. Poll ``GET /documents/{job_id}`` for a queued document's progress.
    """

    name: str
    status: str  # queued | rejected
    job_id: str | None = None
    error: str | None = None
