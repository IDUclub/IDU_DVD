"""Document-level read API (MSI-TSIM-facing): list documents, fetch one by doc_id, resolve by key.

Complements semantic search with direct access to a document's assembled text + metadata +
ordered fragments — what a consumer needs to hydrate its own derived entities.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DocumentDetail,
    DocumentFragment,
    DocumentList,
    DocumentUpdateRequest,
    DocumentUpdateResponse,
    FragmentUpdateRequest,
)
from src.dvd_service.services.dvd_service import DocumentEditorService, LibraryService

router = APIRouter(prefix="/library", tags=["library"])


@router.get("/documents", response_model=DocumentList)
async def list_documents(
    library: LibraryService = Depends(Dependencies.get_library),
):
    """All documents in the store with their identity/corpus metadata."""
    return await run_in_threadpool(library.list_documents)


@router.get("/lookup", response_model=DocumentList)
async def find_documents(
    key: str = Query(..., description="exact lookup key or external id value"),
    library: LibraryService = Depends(Dependencies.get_library),
):
    """Resolve documents by an exact lookup key / external id (e.g. a normative code)."""
    return await run_in_threadpool(library.find_documents, key)


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(
    doc_id: str,
    library: LibraryService = Depends(Dependencies.get_library),
):
    """A document by id: assembled text + metadata + ordered fragments (with source grounding)."""
    detail = await run_in_threadpool(library.get_document, doc_id)
    if detail is None:
        raise HTTPException(404, "document not found")
    return detail


@router.patch("/documents/{doc_id}", response_model=DocumentUpdateResponse)
async def update_document_metadata(
    doc_id: str,
    body: DocumentUpdateRequest = Body(...),
    editor: DocumentEditorService = Depends(Dependencies.get_editor),
):
    """Manually update metadata/tags on every fragment belonging to a document."""
    try:
        return await run_in_threadpool(
            editor.update_document, doc_id, body.model_dump(exclude_unset=True)
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0]))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.patch(
    "/documents/{doc_id}/fragments/{fragment_id}", response_model=DocumentFragment
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
