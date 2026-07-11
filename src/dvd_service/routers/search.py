"""Search endpoints: vector search over texts, tables, or all entities."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from src.dependencies import Dependencies
from src.dvd_service.dto import SearchRequest, SearchResponse, TagsResponse
from src.dvd_service.services.dvd_service import SearchService, TagsService

router = APIRouter(tags=["search"])


@router.post("/search/texts", response_model=SearchResponse)
async def search_texts(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Relevant text fragments (kind=text) with filters and context height.

    Set ``user_id``+``scenario_id`` to also (or, with ``include_shared=false``, only) search a
    user document index — see ``/search/user-index/texts`` for the index-only shortcut.
    """
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
    """All unique tags present in the shared document collection, sorted alphabetically."""
    return await run_in_threadpool(tags_svc.get_tags)


def _require_user_index_scope(req: SearchRequest) -> SearchRequest:
    if not req.user_id or not req.scenario_id:
        raise HTTPException(
            400, "user_id and scenario_id are required for index-only search"
        )
    return req.model_copy(update={"include_shared": False})


@router.post("/search/user-index/texts", response_model=SearchResponse)
async def search_user_index_texts(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Search only a user document index (text fragments) — never the shared corpus."""
    return await run_in_threadpool(
        search.search, _require_user_index_scope(req), "text"
    )


@router.post("/search/user-index/tables", response_model=SearchResponse)
async def search_user_index_tables(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Search only a user document index (tables) — never the shared corpus."""
    return await run_in_threadpool(
        search.search, _require_user_index_scope(req), "table"
    )


@router.post("/search/user-index", response_model=SearchResponse)
async def search_user_index_all(
    req: SearchRequest, search: SearchService = Depends(Dependencies.get_search)
):
    """Search only a user document index (all entities) — never the shared corpus."""
    return await run_in_threadpool(search.search, _require_user_index_scope(req), None)
