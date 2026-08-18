"""Search endpoints: vector search over texts, tables, or all entities."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from src.api_clients import ScenarioNotFound, UrbanApiError
from src.common.auth import (
    get_current_user_id,
    get_optional_user_id,
    require_service_token,
)
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    ScopesResponse,
    SearchRequest,
    SearchResponse,
    TagsResponse,
)
from src.dvd_service.services.dvd_service import SearchService, TagsService

router = APIRouter(tags=["search"], dependencies=[Depends(require_service_token)])


async def _run_search(
    search: SearchService,
    req: SearchRequest,
    kind: str | None,
    user_id: str | None,
):
    if req.user_id or req.project_id or req.scenario_id:
        if not user_id:
            raise HTTPException(401, "X-User-Id header is required for user data")
        req = req.model_copy(update={"user_id": user_id})
    try:
        return await run_in_threadpool(search.search, req, kind)
    except ScenarioNotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except UrbanApiError as exc:
        raise HTTPException(502, str(exc))


@router.post("/search/texts", response_model=SearchResponse)
async def search_texts(
    req: SearchRequest,
    search: SearchService = Depends(Dependencies.get_search),
    user_id: str | None = Depends(get_optional_user_id),
):
    """Relevant text fragments (kind=text) with filters and context height.

    Set ``user_id``+``scenario_id`` to also (or, with ``include_shared=false``, only) search a
    user document index — see ``/search/user-index/texts`` for the index-only shortcut.
    """
    return await _run_search(search, req, "text", user_id)


@router.post("/search/tables", response_model=SearchResponse)
async def search_tables(
    req: SearchRequest,
    search: SearchService = Depends(Dependencies.get_search),
    user_id: str | None = Depends(get_optional_user_id),
):
    """Relevant tables (kind=table) — stored as separate entities."""
    return await _run_search(search, req, "table", user_id)


@router.post("/search", response_model=SearchResponse)
async def search_all(
    req: SearchRequest,
    search: SearchService = Depends(Dependencies.get_search),
    user_id: str | None = Depends(get_optional_user_id),
):
    """Search across all entities (texts and tables)."""
    return await _run_search(search, req, None, user_id)


@router.get("/tags", response_model=TagsResponse)
async def get_tags(tags_svc: TagsService = Depends(Dependencies.get_tags)):
    """All unique tags present in the shared document collection, sorted alphabetically."""
    return await run_in_threadpool(tags_svc.get_tags)


@router.get("/scopes", response_model=ScopesResponse)
async def get_scopes(tags_svc: TagsService = Depends(Dependencies.get_tags)):
    """Document levels and territories actually present in the collection, with counts.

    What a filter control (or an agent) should offer: territories with no documents behind
    them would only produce empty result sets.
    """
    return await run_in_threadpool(tags_svc.get_scopes)


def _require_user_index_scope(req: SearchRequest) -> SearchRequest:
    if not req.user_id or not (req.project_id or req.scenario_id):
        raise HTTPException(
            400,
            "user_id and one of project_id or scenario_id are required for index-only search",
        )
    return req.model_copy(update={"include_shared": False})


@router.post("/search/user-index/texts", response_model=SearchResponse)
async def search_user_index_texts(
    req: SearchRequest,
    search: SearchService = Depends(Dependencies.get_search),
    user_id: str = Depends(get_current_user_id),
):
    """Search only a user document index (text fragments) — never the shared corpus."""
    req = req.model_copy(update={"user_id": user_id})
    return await _run_search(search, _require_user_index_scope(req), "text", user_id)


@router.post("/search/user-index/tables", response_model=SearchResponse)
async def search_user_index_tables(
    req: SearchRequest,
    search: SearchService = Depends(Dependencies.get_search),
    user_id: str = Depends(get_current_user_id),
):
    """Search only a user document index (tables) — never the shared corpus."""
    req = req.model_copy(update={"user_id": user_id})
    return await _run_search(search, _require_user_index_scope(req), "table", user_id)


@router.post("/search/user-index", response_model=SearchResponse)
async def search_user_index_all(
    req: SearchRequest,
    search: SearchService = Depends(Dependencies.get_search),
    user_id: str = Depends(get_current_user_id),
):
    """Search only a user document index (all entities) — never the shared corpus."""
    req = req.model_copy(update={"user_id": user_id})
    return await _run_search(search, _require_user_index_scope(req), None, user_id)
