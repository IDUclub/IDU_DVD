"""Read-side view of a document's administrative scope.

The write-side definition lives on :class:`NodePayload`; this mirrors it for the API models
(search hits, document listings, library details) so the field names a caller filters by are
the field names it reads back. Inherited rather than repeated — three DTOs, one definition.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdministrativeScope(BaseModel):
    document_level: str | None = None  # federal | regional | municipal
    territory_id: int | None = None
    territory_name: str | None = None
    territory_type_id: int | None = None
    territory_type_name: str | None = None
    territory_path: list[int] = Field(default_factory=list)  # ancestors, root first
    level_source: str = "unset"  # manual | auto | unset
    territory_source: str = "unset"
    territory_confidence: float | None = None
    tagging_status: str = "pending"  # ok | pending
    tagging_error: str | None = None

    @classmethod
    def fields_from(cls, payload: dict) -> dict:
        """The scope slice of a stored payload, defaulted — old points lack these keys."""
        return {
            name: payload.get(name, field.default)
            for name, field in cls.model_fields.items()
            if name != "territory_path"
        } | {"territory_path": payload.get("territory_path") or []}
