"""Document-level read API (MSI-TSIM-facing): list documents, fetch one by doc_id, resolve by key.

Complements semantic search with direct access to a document's assembled text + metadata +
ordered fragments — what a consumer needs to hydrate its own derived entities.
"""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from src.api_clients import TerritoryNotFound, UrbanApiError
from src.common.auth import (
    get_effective_user_id,
    require_admin,
    require_authenticated,
)
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DocumentDetail,
    DocumentFragment,
    DocumentList,
    DocumentRelations,
    DocumentUpdateRequest,
    DocumentUpdateResponse,
    FragmentUpdateRequest,
    NodeDetail,
)
from src.dvd_service.dto.fragment_search import NameBackfillRequest
from src.dvd_service.routers._scenario_scope import (
    SCENARIO_FILTER_DESCRIPTION,
    SCENARIO_ID_DESCRIPTION,
    scenario_condition,
)
from src.dvd_service.services.dvd_service import DocumentEditorService, LibraryService

router = APIRouter(prefix="/library", tags=["library"])


@router.post("/fragment-names/backfill", dependencies=[Depends(require_admin)])
async def backfill_fragment_names(
    req: NameBackfillRequest,
):
    """Preview/apply one resumable metadata-only page. Default dry_run=true; vectors stay intact."""
    from src.dependencies import Dependencies
    from src.dvd_service.services.fragment_search import FragmentSearchService

    try:
        return await run_in_threadpool(
            FragmentSearchService(Dependencies.get_search()).backfill, req
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# Reading the library is open to any live token; hand-editing the shared corpus is not.
AUTHENTICATED = [Depends(require_authenticated)]
ADMIN_ONLY = [Depends(require_admin)]


@router.get("/documents", response_model=DocumentList, dependencies=AUTHENTICATED)
async def list_documents(
    document_level: str | None = Query(
        None, description="federal | regional | municipal"
    ),
    territory_ids: list[int] | None = Query(
        None,
        description="Urban API territory ids; matches the territory or anything above it",
    ),
    tagging_status: str | None = Query(None, description="ok | pending"),
    scenario_id: str | None = Query(None, description=SCENARIO_ID_DESCRIPTION),
    scenario_territory_filter: bool = Query(
        True, description=SCENARIO_FILTER_DESCRIPTION
    ),
    library: LibraryService = Depends(Dependencies.get_library),
    user_id: str | None = Depends(get_effective_user_id),
):
    """All documents in the store with their identity/corpus/scope metadata.

    The administrative-scope filters narrow the listing the same way they narrow search:
    ``territory_ids`` matches the stored ancestor chain, so a municipality also brings back
    the regional and federal documents in force there; ``scenario_id`` does the same for the
    territories under the scenario's project boundary.
    """
    condition = await scenario_condition(
        scenario_id, user_id, territory_ids, scenario_territory_filter
    )
    return await run_in_threadpool(
        partial(
            library.list_documents,
            document_level=document_level,
            territory_ids=territory_ids,
            tagging_status=tagging_status,
            scenario_condition=condition,
        )
    )


@router.get("/lookup", response_model=DocumentList, dependencies=AUTHENTICATED)
async def find_documents(
    key: str = Query(..., description="exact lookup key or external id value"),
    library: LibraryService = Depends(Dependencies.get_library),
):
    """Resolve documents by an exact lookup key / external id (e.g. a normative code)."""
    return await run_in_threadpool(library.find_documents, key)


@router.get(
    "/documents/{doc_id}", response_model=DocumentDetail, dependencies=AUTHENTICATED
)
async def get_document(
    doc_id: str,
    include_superseded: bool = Query(
        False,
        description="Also return fragments of editions replaced by a consolidated one",
    ),
    library: LibraryService = Depends(Dependencies.get_library),
):
    """A document by id: assembled text + metadata + ordered fragments (with source grounding).

    Fragments of superseded editions (replaced by one with its amendments applied) are left
    out by default.
    """
    detail = await run_in_threadpool(library.get_document, doc_id, include_superseded)
    if detail is None:
        raise HTTPException(404, "document not found")
    return detail


@router.get(
    "/documents/{doc_id}/relations",
    response_model=DocumentRelations,
    dependencies=AUTHENTICATED,
)
async def get_document_relations(
    doc_id: str,
    min_weight: float = Query(
        0.0, ge=0.0, le=1.0, description="drop weaker directed relations"
    ),
    library: LibraryService = Depends(Dependencies.get_library),
):
    """Directed semantic dependencies between the document's fragments.

    ``source_id`` depends on ``target_id`` with ``weight`` (0..1): reading the target is
    needed to understand or apply the source. Consumers building derived layers (e.g. the
    restriction graph) take these as edges between the fragments they already hold.
    """
    relations = await run_in_threadpool(library.get_relations, doc_id, min_weight)
    if relations is None:
        raise HTTPException(404, "document not found")
    return relations


@router.get("/nodes/{node_id}", response_model=NodeDetail, dependencies=AUTHENTICATED)
async def get_node(
    node_id: str,
    with_children: bool = Query(True, description="resolve child fragments"),
    with_neighbours: bool = Query(True, description="resolve reading-order neighbours"),
    library: LibraryService = Depends(Dependencies.get_library),
):
    """One fragment with its parent, children and neighbours — widen a search hit's context.

    Lets a caller follow the ids a search hit already carries without fetching the whole
    document; for a table row it is how you get back to the table (the table node keeps the
    complete ``table_html``).
    """
    node = await run_in_threadpool(
        library.get_node, node_id, with_children, with_neighbours
    )
    if node is None:
        raise HTTPException(404, "node not found")
    return node


@router.patch(
    "/documents/{doc_id}",
    response_model=DocumentUpdateResponse,
    dependencies=ADMIN_ONLY,
)
async def update_document_metadata(
    doc_id: str,
    body: DocumentUpdateRequest = Body(...),
    editor: DocumentEditorService = Depends(Dependencies.get_editor),
):
    """Manually update metadata/tags on every fragment belonging to a document.

    ``territory_id`` is resolved against the Urban API before anything is written, so an
    unknown territory answers 404 and an unreachable Urban API answers 502 — an explicit
    manual choice is never stored half-resolved.
    """
    try:
        return await run_in_threadpool(
            editor.update_document, doc_id, body.model_dump(exclude_unset=True)
        )
    except TerritoryNotFound as exc:
        raise HTTPException(404, f"территория не найдена в Urban API: {exc}")
    except UrbanApiError as exc:
        raise HTTPException(502, f"Urban API недоступен: {exc}")
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.patch(
    "/documents/{doc_id}/fragments/{fragment_id}",
    response_model=DocumentFragment,
    dependencies=ADMIN_ONLY,
)
async def update_document_fragment(
    doc_id: str,
    fragment_id: str,
    body: FragmentUpdateRequest = Body(...),
    editor: DocumentEditorService = Depends(Dependencies.get_editor),
):
    """Edit one fragment; changing text recalculates and atomically stores its embedding."""
    try:
        result = await run_in_threadpool(
            editor.update_fragment,
            doc_id,
            fragment_id,
            body.model_dump(exclude_unset=True),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return DocumentFragment(**result)
