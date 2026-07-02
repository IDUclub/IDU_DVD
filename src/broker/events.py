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
    """Model for message indicates that a document has been fully processed
    and stored in the vector database."""

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
