"""Search endpoints: vector search over texts, tables, or all entities."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from src.dependencies import Dependencies
from src.dvd_service.dto import SearchRequest, SearchResponse, TagsResponse
from src.dvd_service.services.dvd_service import SearchService, TagsService

router = APIRouter(tags=["search"])


@router.post("/search/texts", response_model=SearchResponse)
async def search_texts(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Relevant text fragments (kind=text) with filters and context height."""
    return await run_in_threadpool(search.search, req, "text")


@router.post("/search/tables", response_model=SearchResponse)
async def search_tables(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Relevant tables (kind=table) — stored as separate entities."""
    return await run_in_threadpool(search.search, req, "table")


@router.post("/search", response_model=SearchResponse)
async def search_all(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Search across all entities (texts and tables)."""
    return await run_in_threadpool(search.search, req, None)


@router.get("/tags", response_model=TagsResponse)
async def get_tags(tags_svc: TagsService = Depends(Dependencies.get_tags)):
    """All unique tags present in the document collection, sorted alphabetically."""
    return await run_in_threadpool(tags_svc.get_tags)
