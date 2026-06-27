# Architecture

## Overview

The application is a FastAPI service that orchestrates the document processing chain and stores the
result in Qdrant. Heavy work (document parsing, structure markup, tagging, vectorization) runs in
the background; background job statuses and the document registry live in Redis; the large language
model and the embedding model are called through Ollama.

## Stack

| Component | Role |
|-----------|------|
| FastAPI | HTTP API, background tasks |
| Qdrant | vector database; a single collection, payload indexes |
| Redis | parsing job statuses, document and version registry |
| Ollama | LLM (markup, merge, tags, version) and embeddings |
| unstructured (python-docx) | text and table extraction from `.docx` |
| pydantic-settings | configuration via environment variables |
| structlog | structured logging |

## OOP and the dependency container

Every functional module is a separate class. All objects are assembled into a `Dependencies`
container by `init_dependencies()` (`src/dependencies/init_dependencies.py`). `Dependencies` is a
singleton: it is populated once at application startup in `lifespan` and is then available from
anywhere. Endpoints receive individual dependencies through FastAPI getters, e.g.
`Depends(Dependencies.get_search)`; the whole container is also available via `get_dependencies()`.

The pipeline-stage classes keep no state between documents. The Ollama client (`OllamaClient`) is
created inside the services per operation, which keeps background processing thread-safe.

Container contents:

```
Dependencies(
    settings, qdrant, redis, jobs, registry,
    parser, structure, hierarchy, tagger, version_detector,
    ingestion, search,
)
```

## Project structure

| Path | Contents |
|------|----------|
| `src/common/config/app_config.py` | `Settings` — configuration (pydantic-settings) |
| `src/api_clients/ollama_client.py` | `OllamaClient`, `OllamaError` |
| `src/common/db/qdrant_client.py` | `QdrantRepository` |
| `src/common/db/redis_client.py` | `RedisClient`, `JobStore`, `DocumentRegistry` |
| `src/dvd_service/modules/doc_parsers.py` | `DocumentParser` (Stages 1 and 1.5) |
| `src/dvd_service/modules/structure.py` | `StructureTagger` (Stages 2, 3, 3.5) |
| `src/dvd_service/modules/hierarchy.py` | `HierarchyBuilder` (Stage 4 and node flattening) |
| `src/dvd_service/modules/tagging.py` | `Tagger`, `VersionDetector` |
| `src/dvd_service/modules/windowing.py` | `make_windows`, `reconcile` |
| `src/dvd_service/services/dvd_service.py` | `IngestionService`, `SearchService` |
| `src/dvd_service/dto/` | `NodePayload` (`node_payload.py`) and request/response DTOs (`upload.py`, `search.py`) |
| `src/dvd_service/routers/` | HTTP endpoints (`documents.py`, `search.py`) |
| `src/dependencies/dependencies.py` | `Dependencies` (singleton) and getters |
| `src/dependencies/init_dependencies.py` | `init_dependencies` |
| `src/mcp_server/server.py` | MCP server (fastmcp): getter tools |
| `src/mcp_server/app.py` | the MCP server's ASGI app for mounting |
| `src/main.py` | application assembly, `lifespan` |
| `src/dev_runner.py` | uvicorn launcher for development |

## Classes

### Infrastructure

- `OllamaClient` — synchronous Ollama client. `chat(system, user, schema)` performs a request with
  a strict JSON response schema; `embed(texts)` returns vectors; `available()` checks availability.
- `QdrantRepository` — a wrapper over the Qdrant client: `ensure_collection()` (idempotent creation
  of the collection and payload indexes), `upsert(points)`, `search(vector, filter, limit)`,
  `retrieve(ids)`, `set_other_versions(name, version, other_versions)`.
- `RedisClient` — Redis connection. `JobStore` — job statuses (`dvd:job:{id}`).
  `DocumentRegistry` — document hashes for deduplication (`dvd:hash:{hash}`) and version sets per
  document name (`dvd:versions:{name}`).

### Pipeline

- `DocumentParser` — extraction from `.docx` (`extract_raw`), full-text hash (`content_hash`),
  splitting and stitching into logical parts (`to_logical_parts`, `semantic_merge`).
- `StructureTagger` — structure markup (`tag`), type normalization (`categorize`), removal of the
  duplicated number from the text (`strip_leading_numbering`), numbering rank
  (`numbering_rank`, `numbering_ranks`).
- `HierarchyBuilder` — tree building (`build`), post-validation (`cap_unnumbered_nesting`),
  amendment grouping (`group_amendment`), flattening into nodes (`flatten`).
- `Tagger` — fragment tagging (`tag_nodes`).
- `VersionDetector` — document name and version detection (`detect`).

### Services

- `IngestionService.ingest(file_path, raw, content_hash, ...)` — the full processing pipeline and
  ingestion of nodes into Qdrant.
- `SearchService.search(request, kind)` — query vectorization, filtering, search and context
  assembly from neighbouring fragments.

## MCP

`src/mcp_server/server.py` exposes the application's read-only getters as MCP tools (fastmcp) on top
of the same `Dependencies` container — without a separate DB/Redis initialization:

- `search_texts`, `search_tables`, `search_all` — wrappers over `SearchService.search`.
- `job_status` — a wrapper over `JobStore.get`.
- `document_versions` — a wrapper over `DocumentRegistry.versions`.

The MCP server's ASGI app (`src/mcp_server/app.py`) is mounted into the main FastAPI application
(`src/main.py`) at the `/mcp` path (streamable HTTP transport); the MCP server's `lifespan` is
merged with the application's `lifespan`, so both start in a single process.

## Data model

Each document node is a separate Qdrant point. The point id is a UUID. The `text` field is
vectorized. Payload contents (`NodePayload`):

| Field | Type | Description |
|-------|------|-------------|
| `doc_id` | str | document upload identifier |
| `name` | str | document designation (e.g. "SP 19.13330.2019") |
| `version` | str | version/revision |
| `other_versions` | list[str] | other versions of this document in the store |
| `content_hash` | str | full document-text hash |
| `source` | str | source file name |
| `kind` | str | `text` or `table` |
| `type` | str | structural element type |
| `numbering` | str | the fragment's own number |
| `block` | str | `main` or `amendment` |
| `depth` | int | depth in the hierarchy |
| `parent_id` | str | parent node identifier |
| `parent_text` | str | parent text |
| `child_ids` | list[str] | child node identifiers |
| `prev_id` | str | previous fragment in reading order |
| `next_id` | str | next fragment in reading order |
| `breadcrumb` | str | path from the root (section / clause) |
| `tags` | list[str] | tags |
| `table_html` | str | HTML representation of a table (for `kind=table`) |
| `references` | list[DocumentRef] | outgoing links to other documents/clauses (see below) |
| `text` | str | fragment text |

Each entry of `references` is a `DocumentRef`: `raw` (verbatim text of the reference),
`target_name` / `target_numbering` (the referenced designation and clause), `scope`
(`internal`/`external`), the resolved `target_doc_id` / `target_version` / `target_node_id`
(Qdrant point id of the exact clause), and `resolved`.

Payload indexes are created on `doc_id`, `name`, `version`, `kind`, `type`, `block`, `parent_id`,
`content_hash`, `tags`, `numbering`, and `references[].target_name`.

## Storage

- Qdrant: a single collection (default `documents`), vector of size `vector_size`, cosine metric.
  Texts and tables live in the same collection and are distinguished by the `kind` field.
- Redis: job statuses (`dvd:job:{job_id}`, with TTL), the hash registry (`dvd:hash:{hash}`),
  versions (`dvd:versions:{name}`), the set of all document names (`dvd:names`, for reference
  matching) and the pending-reference queues (`dvd:pending_ref:{normalized_name}`).
- Learned reference patterns live in a separate, durable Qdrant collection (default `ref_patterns`,
  dummy 1-d vectors used as a key/value store), so they survive a Redis wipe; the seed patterns are
  committed in `reference_patterns.py`.
