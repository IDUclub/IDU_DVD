"""API DTOs for user-scoped document indices, keyed by (user_id, scenario_id)."""

from __future__ import annotations

from pydantic import BaseModel


class UserIndexCreateRequest(BaseModel):
    user_id: str
    scenario_id: str
    project_id: str
    parent_scenario_id: str | None = None  # scenario to inherit documents from (live, recursive)


class UserIndexInfo(BaseModel):
    user_id: str
    scenario_id: str
    project_id: str
    parent_scenario_id: str | None = None
    created_at: str
    document_count: int = 0  # this scenario's own documents, not counting inherited ones


class UserIndexListResponse(BaseModel):
    count: int
    indices: list[UserIndexInfo]


class UserIndexDeleteResponse(BaseModel):
    user_id: str
    scenario_id: str
    points_deleted: int
