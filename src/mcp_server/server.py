"""MCP server: read-only tools over the application getters (search, statuses, versions).

Mounted into the main FastAPI application (`src/main.py`) at the `/mcp` path and reuses the same
`Dependencies` container as the HTTP routers — it requires no separate DB/Redis initialization.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DeleteResponse,
    DocumentDetail,
    DocumentList,
    DocumentListResponse,
    NodeDetail,
    ScopesResponse,
    SearchRequest,
    SearchResponse,
    TagsResponse,
    UserIndexDeleteResponse,
    UserIndexInfo,
    UserIndexListResponse,
)
from src.dvd_service.modules.reference_patterns import normalize_designation
from src.dvd_service.services.dvd_service import DocumentsService
from src.dvd_service.services.user_index_service import build_user_ingestion_from_deps

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
    document_names: list[str] | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    scenario_id: str | None = None,
    include_shared: bool = True,
    include_inherited: bool = True,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    req = SearchRequest(
        query=query,
        name=name,
        version=version,
        block=block,
        types=types,
        tags=tags,
        document_names=document_names,
        limit=limit,
        context_height=context_height,
        user_id=user_id,
        project_id=project_id,
        scenario_id=scenario_id,
        include_shared=include_shared,
        include_inherited=include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )
    return Dependencies.get_search().search(req, kind)


@mcp.tool()
def search_texts(
    query: str,
    name: str | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
    user_id: str | None = None,
    project_id: str | None = None,
    scenario_id: str | None = None,
    include_shared: bool = True,
    include_inherited: bool = True,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    """Vector search over text fragments (kind=text) with filters and context height.

    ``block`` filters by main/amendment, ``types`` by structural level (chapter/clause/
    subclause/...). ``document_names`` restricts results to any of the given document names.
    ``parent_id`` searches only among one node's direct children — drill into a clause or a
    table you already found (get its id from a previous hit or from get_node).

    Narrow by administrative scope with ``document_level`` (federal/regional/municipal)
    and ``territory_ids`` — Urban API territory ids from ``get_document_scopes``, which
    match both a territory's own documents and the higher-level ones in force there.
    Set ``user_id``+``scenario_id`` to also search a user document index (combined search);
    add ``include_shared=False`` to search only that index, or ``include_inherited=False`` to
    skip its inheritance chain.
    """
    return _search(
        query,
        name,
        version,
        tags,
        limit,
        context_height,
        "text",
        block,
        types,
        document_names,
        user_id,
        project_id,
        scenario_id,
        include_shared,
        include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )


@mcp.tool()
def search_tables(
    query: str,
    name: str | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
    user_id: str | None = None,
    project_id: str | None = None,
    scenario_id: str | None = None,
    include_shared: bool = True,
    include_inherited: bool = True,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    """Vector search over tables (kind=table) with filters and context height.

    ``document_names`` restricts results to any of the given document names.
    ``parent_id`` searches only inside one node — pass a table's id to search within that
    table rather than across all of them; ``get_node`` on the same id returns it whole.
    Set ``user_id``+``scenario_id`` to also search a user document index (combined search); add
    ``include_shared=False`` to search only that index, or ``include_inherited=False`` to skip
    its inheritance chain.
    """
    return _search(
        query,
        name,
        version,
        tags,
        limit,
        context_height,
        "table",
        block,
        types,
        document_names,
        user_id,
        project_id,
        scenario_id,
        include_shared,
        include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )


@mcp.tool()
def search_all(
    query: str,
    name: str | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
    user_id: str | None = None,
    project_id: str | None = None,
    scenario_id: str | None = None,
    include_shared: bool = True,
    include_inherited: bool = True,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    """Vector search across all entities (texts and tables) with filters and context height.

    ``document_names`` restricts results to any of the given document names. Set
    ``user_id``+``scenario_id`` to also search a user document index (combined search); add
    ``include_shared=False`` to search only that index, or ``include_inherited=False`` to skip
    its inheritance chain.

    Narrow by administrative scope with ``document_level`` (federal/regional/municipal)
    and ``territory_ids`` — Urban API territory ids from ``get_document_scopes``, which
    match both a territory's own documents and the higher-level ones in force there.
    """
    return _search(
        query,
        name,
        version,
        tags,
        limit,
        context_height,
        None,
        block,
        types,
        document_names,
        user_id,
        project_id,
        scenario_id,
        include_shared,
        include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )


@mcp.tool()
def search_user_index_texts(
    user_id: str,
    scenario_id: str,
    query: str,
    project_id: str | None = None,
    include_inherited: bool = True,
    name: str | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    """Vector search restricted to a user document index (text fragments) — never the shared corpus."""
    return _search(
        query,
        name,
        version,
        tags,
        limit,
        context_height,
        "text",
        block,
        types,
        document_names,
        user_id,
        project_id,
        scenario_id,
        False,
        include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )


@mcp.tool()
def search_user_index_tables(
    user_id: str,
    scenario_id: str,
    query: str,
    project_id: str | None = None,
    include_inherited: bool = True,
    name: str | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    """Vector search restricted to a user document index (tables) — never the shared corpus."""
    return _search(
        query,
        name,
        version,
        tags,
        limit,
        context_height,
        "table",
        block,
        types,
        document_names,
        user_id,
        project_id,
        scenario_id,
        False,
        include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )


@mcp.tool()
def search_user_index_all(
    user_id: str,
    scenario_id: str,
    query: str,
    project_id: str | None = None,
    include_inherited: bool = True,
    name: str | None = None,
    document_names: list[str] | None = None,
    version: str | None = None,
    block: str | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    context_height: int = 0,
    parent_id: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
) -> SearchResponse:
    """Vector search restricted to a user document index (all entities) — never the shared corpus."""
    return _search(
        query,
        name,
        version,
        tags,
        limit,
        context_height,
        None,
        block,
        types,
        document_names,
        user_id,
        project_id,
        scenario_id,
        False,
        include_inherited,
        parent_id=parent_id,
        document_level=document_level,
        territory_ids=territory_ids,
    )


@mcp.tool()
def list_documents(
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    tags: list[str] | None = None,
    uploaded_from: str | None = None,
    uploaded_to: str | None = None,
    document_level: str | None = None,
    territory_ids: list[int] | None = None,
    tagging_status: str | None = None,
) -> DocumentListResponse:
    """Documents already in the shared store, aggregated by (name, version), with optional filters.

    ``document_level`` is federal/regional/municipal; ``territory_ids`` are Urban API territory
    ids (get them from ``get_document_scopes``) and match both the documents of a territory and
    the higher-level documents in force on it.
    """
    return Dependencies.get_documents().list_documents(
        name,
        version,
        block,
        tags,
        uploaded_from,
        uploaded_to,
        document_level=document_level,
        territory_ids=territory_ids,
        tagging_status=tagging_status,
    )


@mcp.tool()
def create_user_index(
    user_id: str,
    scenario_id: str,
    project_id: str,
    parent_scenario_id: str | None = None,
) -> UserIndexInfo:
    """Create a user document index — a (user_id, scenario_id) bucket, optionally inheriting
    (live, recursively) from another scenario's index."""
    try:
        return Dependencies.get_user_index_service().create_index(
            user_id, scenario_id, project_id, parent_scenario_id
        )
    except ValueError as exc:
        raise ToolError(str(exc))


@mcp.tool()
def list_user_indices(user_id: str) -> UserIndexListResponse:
    """All document indices belonging to a user."""
    return Dependencies.get_user_index_service().list_indices(user_id)


@mcp.tool()
def delete_user_index(user_id: str, scenario_id: str) -> UserIndexDeleteResponse:
    """Wipe a user document index entirely (inherited documents are untouched)."""
    try:
        return Dependencies.get_user_index_service().delete_index(user_id, scenario_id)
    except KeyError as exc:
        raise ToolError(str(exc.args[0]) if exc.args else "index not found")


@mcp.tool()
def list_user_documents(
    user_id: str,
    scenario_id: str,
    include_inherited: bool = True,
    name: str | None = None,
    version: str | None = None,
    block: str | None = None,
    tags: list[str] | None = None,
    uploaded_from: str | None = None,
    uploaded_to: str | None = None,
) -> DocumentListResponse:
    """Documents in a user index, aggregated by (name, version) — includes the scenario's
    inheritance chain by default."""
    index_registry = Dependencies.get_user_index_registry()
    scenario_ids = (
        index_registry.ancestor_chain(user_id, scenario_id)
        if include_inherited
        else [scenario_id]
    )
    documents = DocumentsService(Dependencies.get_qdrant())
    return documents.list_documents(
        name,
        version,
        block,
        tags,
        uploaded_from,
        uploaded_to,
        user_id=user_id,
        scenario_ids=scenario_ids,
    )


@mcp.tool()
def delete_user_document(
    user_id: str, scenario_id: str, name: str, version: str | None = None
) -> DeleteResponse:
    """Delete a document (or one of its versions) from a user index."""
    deps = Dependencies.instance()
    record = deps.user_index_registry.get(user_id, scenario_id)
    if record is None:
        raise ToolError(f"index not found: {user_id}/{scenario_id}")
    ingestion = build_user_ingestion_from_deps(
        deps, user_id=user_id, project_id=record["project_id"], scenario_id=scenario_id
    )
    try:
        result = ingestion.delete_document(name, version)
    except KeyError as exc:
        raise ToolError(str(exc.args[0]) if exc.args else "document not found")
    return DeleteResponse(**result)


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
def get_node(
    node_id: str,
    with_children: bool = True,
    with_neighbours: bool = True,
) -> NodeDetail:
    """One fragment with its parent, children and reading-order neighbours resolved.

    Use this to widen a search hit instead of get_document: search returns parent_id,
    child_ids, prev_id and next_id, and this follows them one step at a time. For a table
    row, fetching its parent returns the whole table as table_html — complete regardless of
    how the rows were chunked for search.
    """
    node = Dependencies.get_library().get_node(node_id, with_children, with_neighbours)
    if node is None:
        raise ToolError(f"node not found: {node_id}")
    return node


@mcp.tool()
def find_document(key: str) -> DocumentList:
    """Resolve documents by an exact lookup key or external id value (e.g. a normative code)."""
    return Dependencies.get_library().find_documents(key)


@mcp.tool()
def get_tags() -> TagsResponse:
    """All unique tags present in the document collection, sorted alphabetically."""
    return Dependencies.get_tags().get_tags()


@mcp.tool()
def get_document_scopes() -> ScopesResponse:
    """Document levels and territories the collection actually holds, with document counts.

    Call this before filtering by ``document_level`` / ``territory_ids``: it is where the
    territory ids come from. Only territories that have documents behind them are listed, so
    every value it returns is a filter that returns something.
    """
    return Dependencies.get_tags().get_scopes()
