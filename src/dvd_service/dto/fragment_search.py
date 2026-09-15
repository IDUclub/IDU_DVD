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
    rank_by_relevance: bool = False
    kind: Literal["all", "text", "table"] = "all"
    allow_multiple: bool = False
    root_ids: list[str] | None = Field(None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_selector(self):
        self.pattern = (self.pattern or "").strip() or None
        self.name_query = (self.name_query or "").strip() or None
        if not self.pattern and not self.name_query and not self.rank_by_relevance:
            raise ValueError("pattern or name_query is required")
        if self.rank_by_relevance and not self.query.strip():
            raise ValueError("query is required for relevance ranking")
        if self.rank_by_relevance and self.cursor:
            raise ValueError(
                "ranked retrieval returns one bounded result, without a cursor"
            )
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
