"""MCP server: read-only tools over the application getters (search, statuses, versions).

Mounted into the main FastAPI application (`src/main.py`) at the `/mcp` path and reuses the same
`Dependencies` container as the HTTP routers — it requires no separate DB/Redis initialization.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DocumentDetail,
    DocumentList,
    DocumentListResponse,
    SearchRequest,
    SearchResponse,
)
from src.dvd_service.modules.reference_patterns import normalize_designation

mcp = FastMCP("dvd-idu")


def _search(
    query: str,
    name: str | None,
    version: str | None,
    tags: list[str] | None,
    limit: int,
    context_height: int,
    kind: str | None,
    block: str | None = None,
    types: list[str] | None = None,
) -> SearchResponse:
    req = SearchRequest(
        query=query,
        name=name,
        version=version,
        block=block,
        types=types,
        tags=tags,
        limit=limit,
        context_height=context_height,
    )
    return Dependencies.get_search().search(req, kind)


@mcp.tool()
def search_texts(
    query: str,
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
) -> SearchResponse:
    """Vector search over text fragments (kind=text) with filters and context height.

    ``block`` filters by main/amendment, ``types`` by structural level (chapter/clause/
    subclause/...).
    """
    return _search(
        query, name, version, tags, limit, context_height, "text", block, types
    )


@mcp.tool()
def search_tables(
    query: str,
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
) -> SearchResponse:
    """Vector search over tables (kind=table) with filters and context height."""
    return _search(
        query, name, version, tags, limit, context_height, "table", block, types
    )


@mcp.tool()
def search_all(
    query: str,
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
) -> SearchResponse:
    """Vector search across all entities (texts and tables) with filters and context height."""
    return _search(
        query, name, version, tags, limit, context_height, None, block, types
    )


@mcp.tool()
def list_documents(
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    tags: list[str] | None = None,
    uploaded_from: str | None = None,
    uploaded_to: str | None = None,
) -> DocumentListResponse:
    """Documents already in the store, aggregated by (name, version), with optional filters."""
    return Dependencies.get_documents().list_documents(
        name, version, block, tags, uploaded_from, uploaded_to
    )


@mcp.tool()
def job_status(job_id: str) -> dict:
    """Status of a background document upload/parsing job by its id."""
    job = Dependencies.get_jobs().get(job_id)
    if not job:
        raise ToolError(f"job not found: {job_id}")
    return job


@mcp.tool()
def document_versions(name: str) -> list[str]:
    """List of document versions already loaded into the database, by its name/designation."""
    return Dependencies.get_registry().versions(name)


@mcp.tool()
def pending_references(name: str) -> list[dict]:
    """Dangling references awaiting a not-yet-loaded document, by its name/designation.

    These are completed automatically once that document is ingested.
    """
    return Dependencies.get_registry().peek_pending(normalize_designation(name))


@mcp.tool()
def get_document(doc_id: str) -> DocumentDetail:
    """A document by doc_id: assembled text + metadata + ordered fragments with source grounding."""
    detail = Dependencies.get_library().get_document(doc_id)
    if detail is None:
        raise ToolError(f"document not found: {doc_id}")
    return detail


@mcp.tool()
def find_document(key: str) -> DocumentList:
    """Resolve documents by an exact lookup key or external id value (e.g. a normative code)."""
    return Dependencies.get_library().find_documents(key)
