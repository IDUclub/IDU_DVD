"""Kafka event models (otteroad AvroEventModel).

Defined locally for now; the plan is to upstream them into ``otteroad.models``
(a ``document_events`` category) so consumer services import the same schema.
Keep the otteroad model style (ClassVar topic/namespace/versions) to make that
move a copy-paste.
"""

from __future__ import annotations

from typing import ClassVar

from otteroad.avro import AvroEventModel
from pydantic import Field


class DocumentProcessed(AvroEventModel):
    """Model for message indicates that a new document has been fully processed
    and stored in the vector database for the first time."""

    topic: ClassVar[str] = "document.events"
    namespace: ClassVar[str] = "documents"
    schema_version: ClassVar[int] = 1
    schema_compatibility: ClassVar[str] = "BACKWARD"

    # NB: keep descriptions/docstrings ASCII-only — otteroad matches consumed messages
    # to model classes by comparing the registry schema string with json.dumps(schema)
    # (ensure_ascii=True), so any non-ASCII character breaks model resolution.
    document_name: str = Field(
        ...,
        description="unique document name (registry key), enough to fetch all "
        "fragments and versions of the document from the DVD API",
    )


class DocumentUpdated(AvroEventModel):
    """Model for message indicates that a stored document changed in the vector
    database: a new version was indexed (delta update) or the document was fully
    reloaded from scratch."""

    topic: ClassVar[str] = "document.events"
    namespace: ClassVar[str] = "documents"
    schema_version: ClassVar[int] = 1
    schema_compatibility: ClassVar[str] = "BACKWARD"

    document_name: str = Field(
        ...,
        description="unique document name (registry key) of the updated document",
    )
    version: str = Field(
        ...,
        description="version tag the update was indexed under; fragments of this "
        "version are retrievable from the DVD API by name + version",
    )


class DocumentDeleted(AvroEventModel):
    """Model for message indicates that a document (or one of its versions) was
    removed from the vector database."""

    topic: ClassVar[str] = "document.events"
    namespace: ClassVar[str] = "documents"
    schema_version: ClassVar[int] = 1
    schema_compatibility: ClassVar[str] = "BACKWARD"

    document_name: str = Field(
        ...,
        description="unique document name (registry key) the deletion applies to",
    )
    versions_removed: list[str] = Field(
        ...,
        description="version tags removed from the store by this deletion",
    )
    document_removed: bool = Field(
        ...,
        description="true when no versions of the document remain in the store",
    )
