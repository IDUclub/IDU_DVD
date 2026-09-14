"""One contract for REST and MCP structural/name retrieval."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.dvd_service.dto.search import SearchHit, SearchRequest


class FragmentSearchRequest(SearchRequest):
    query: str = ""
    pattern: str | None = Field(None, max_length=256)
    name_query: str | None = Field(None, max_length=256)
    name_mode: Literal["strict", "expanded"] = "strict"
    name_scope: Literal["self", "path"] = "self"
    include_children: bool = True
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = None

    @model_validator(mode="after")
    def require_selector(self):
        self.pattern = (self.pattern or "").strip() or None
        self.name_query = (self.name_query or "").strip() or None
        if not self.pattern and not self.name_query:
            raise ValueError("pattern or name_query is required")
        if not self.include_shared and not (self.project_id or self.scenario_id):
            raise ValueError("include_shared=false requires a project or scenario")
        return self


class FragmentMatch(SearchHit):
    matched: bool = True
    match_kind: str = "structure"
    matched_ancestor_ids: list[str] = Field(default_factory=list)


class FragmentSearchResponse(BaseModel):
    count: int
    total: int
    match_count: int
    hits: list[FragmentMatch]
    next_cursor: str | None = None
    complete: bool
    ambiguous: bool = False
    candidates: list[dict] = Field(default_factory=list)
    candidates_complete: bool = True


class NameBackfillRequest(BaseModel):
    doc_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    dry_run: bool = True
    limit: int = Field(200, ge=1, le=1000)
    cursor: str | None = None

    @model_validator(mode="after")
    def require_scope_pair(self):
        if bool(self.user_id) != bool(self.project_id):
            raise ValueError("private backfill requires both user_id and project_id")
        return self
