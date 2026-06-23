"""MCP server: read-only tools over the application getters (search, statuses, versions).

Mounted into the main FastAPI application (`src/main.py`) at the `/mcp` path and reuses the same
`Dependencies` container as the HTTP routers — it requires no separate DB/Redis initialization.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.dependencies import Dependencies
from src.dvd_service.dto import SearchRequest, SearchResponse

mcp = FastMCP("dvd-idu")


def _search(
    query: str,
    name: str | None,
    version: str | None,
    tags: list[str] | None,
    limit: int,
    context_height: int,
    kind: str | None,
) -> SearchResponse:
    req = SearchRequest(
        query=query,
        name=name,
        version=version,
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
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
) -> SearchResponse:
    """Vector search over text fragments (kind=text) with filters and context height."""
    return _search(query, name, version, tags, limit, context_height, "text")


@mcp.tool()
def search_tables(
    query: str,
    name: str | None = None,
    version: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
) -> SearchResponse:
    """Vector search over tables (kind=table) with filters and context height."""
    return _search(query, name, version, tags, limit, context_height, "table")


@mcp.tool()
def search_all(
    query: str,
    name: str | None = None,
    version: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
) -> SearchResponse:
    """Vector search across all entities (texts and tables) with filters and context height."""
    return _search(query, name, version, tags, limit, context_height, None)


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
