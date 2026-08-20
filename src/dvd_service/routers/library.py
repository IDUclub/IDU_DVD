"""Document-level read API (MSI-TSIM-facing): list documents, fetch one by doc_id, resolve by key.

Complements semantic search with direct access to a document's assembled text + metadata +
ordered fragments — what a consumer needs to hydrate its own derived entities.
"""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from src.api_clients import TerritoryNotFound, UrbanApiError
from src.common.auth import require_authenticated, require_service_token
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DocumentDetail,
    DocumentFragment,
    DocumentList,
    DocumentUpdateRequest,
    DocumentUpdateResponse,
    FragmentUpdateRequest,
    NodeDetail,
)
from src.dvd_service.services.dvd_service import DocumentEditorService, LibraryService

router = APIRouter(prefix="/library", tags=["library"])

# Reading the library is open to any live token; hand-editing the shared corpus is not.
AUTHENTICATED = [Depends(require_authenticated)]
SERVICE_ONLY = [Depends(require_service_token)]


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
    library: LibraryService = Depends(Dependencies.get_library),
):
    """All documents in the store with their identity/corpus/scope metadata.

    The administrative-scope filters narrow the listing the same way they narrow search:
    ``territory_ids`` matches the stored ancestor chain, so a municipality also brings back
    the regional and federal documents in force there.
    """
    return await run_in_threadpool(
        partial(
            library.list_documents,
            document_level=document_level,
            territory_ids=territory_ids,
            tagging_status=tagging_status,
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
    library: LibraryService = Depends(Dependencies.get_library),
):
    """A document by id: assembled text + metadata + ordered fragments (with source grounding)."""
    detail = await run_in_threadpool(library.get_document, doc_id)
    if detail is None:
        raise HTTPException(404, "document not found")
    return detail


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
    dependencies=SERVICE_ONLY,
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
    dependencies=SERVICE_ONLY,
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
