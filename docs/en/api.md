# API

The default base URL is `http://localhost:8000`. Interactive docs (Swagger) are at `/docs`. All
request and response models are pydantic-based and defined under `src/dvd_service/dto/`.

## Authentication

Every endpoint needs a Keycloak bearer token, verified locally against the realm's JWKS —
signature, issuer and `exp`. An expired token is answered with `401 Bearer token has expired`.
There are two gates:

| Gate | Accepts | Applies to |
|------|---------|------------|
| authenticated | any live token — a user's or a service account's | reading the shared corpus: `GET /documents`, `GET /documents/{name}/source`, all of `/library` except its `PATCH`es, all of `/search`, `GET /tags`, `GET /scopes` |
| admin | a user holding the `DVD_ADMIN_ROLE` realm role (`ADMIN` by default) or a service account | everything that changes the shared corpus or the service itself: `POST`/`PATCH`/`PUT`/`DELETE /documents`, `/documents/direct`, the `/documents/jobs/*` views that track those ingests, `PATCH /library/...`, `/tagging`, `/system` |

A user who is authenticated but lacks the role is answered `403`, not `401`: the token is
fine, the person is not entitled. Service accounts are not asked for the role — holding the
client credentials is the authorisation.

The MCP server at `/mcp` requires a service token as a whole.

### The admin panel

`/admin/ui` has no password of its own. Its login form posts the visitor's Keycloak
credentials to the IDU auth helper (`DVD_AUTH_HELPER_URL` + `DVD_AUTH_HELPER_API_KEY`, the
same service behind gMART's `/auth/token`), and the panel opens only for a holder of the admin
role. The session cookie stores the issued access token itself, so the panel's own API calls
are ordinary bearer requests and the session ends when the token expires.

### Who the request acts as

Endpoints that can reach a private user index resolve the acting user themselves, and never
from the request body:

- **A user token acts as itself.** The user id is the token's `sub`; an `X-User-Id` header is
  ignored, and a `user_id` in a search body may only repeat the caller's own id — naming
  anybody else is answered with `403`.
- **A service token may act for a user** by naming them in `X-User-Id`. Without that header a
  service token still reads the shared corpus, but no private index.

## Endpoint list

| Method and path | Purpose |
|-----------------|---------|
| `POST /documents` | upload a document and queue it for processing |
| `PATCH /documents/{name}` | delta update: index a new version, reusing unchanged fragments |
| `PUT /documents/{name}` | full reload: wipe all stored versions and ingest from scratch |
| `DELETE /documents/{name}` | delete a document entirely, or a single version (`?version=`) |
| `POST /documents/direct` | ingest documents directly from caller-supplied fragments (no LLM pipeline) |
| `PUT /documents/direct` | full replace of directly-ingested documents by name |
| `GET /documents` | list ingested documents, aggregated by (name, version), with filters |
| `GET /documents/{job_id}` | processing job status |
| `GET /documents/jobs/active` | queued and currently processing jobs |
| `GET /documents/jobs/recent` | recent jobs of every status (`?limit=20`, max 100) |
| `POST /search/texts` | search relevant text fragments |
| `POST /search/tables` | search relevant tables |
| `POST /search` | search across all entities (texts and tables) |
| `GET /tags` | all unique tags present in the document collection |
| `GET /scopes` | document levels and territories present in the collection, with counts |
| `GET /tagging/pending` | documents whose level/territory is still unresolved |
| `POST /tagging/backfill` | tag pending documents now (background job) |
| `GET /library/documents` | list documents from the registry (identity/corpus metadata) |
| `GET /library/documents/{doc_id}` | one document: assembled text + metadata + ordered fragments |
| `GET /library/nodes/{node_id}` | one fragment with its parent, children and reading-order neighbours |
| `GET /library/lookup` | resolve documents by an exact lookup key / external id |
| `GET /system/logs` | download the application log file (optionally filtered) |
| `GET /system/settings` | read the current effective `DVD_` configuration (secrets masked) |
| `PUT /system/settings` | persist `DVD_` variables to `.env` and apply the runtime-tunable ones |
| `GET /ping` | health check |
| `GET /` | redirect to `/docs` |

## POST /documents

Upload a document. The body is a multipart form.

Form fields:

- `file` — the document file (required);
- `name` — document name/designation to override LLM detection (optional). The name keys the
  version registry and the update/delete endpoints, so setting it explicitly is recommended;
- `version` — a version string to override auto-detection (optional). Without it, the trailing
  standalone 4-digit group of the name is used when present (`СП 2.13130.2020` → `2020`),
  otherwise the version is LLM-detected;
- `doc_type` — document class (`document` / `regulation` / `article` / `book` / `web` / …) (optional);
- `corpus` — logical corpus/namespace the document belongs to (optional);
- `lang` — ISO-639 language code (optional);
- `title` — human-readable title (optional);
- `source_uri` — source file path / URL (optional);
- `effective_date` — effective date (optional);
- `external_ids` — JSON object of caller-supplied ids, e.g. `{"code": "SP 19.13330.2019", "doi": "..."}` (optional);
- `metadata` — JSON object of free-form domain attributes (optional).

All optional metadata is stored on every node of the document, so consumer services can join,
filter, and cite without re-parsing. `external_ids` / `metadata` must be JSON objects (otherwise `422`).

Behaviour:

- Accepted formats are governed by `DVD_ALLOWED_EXTENSIONS` (default `.docx`, `.txt`, `.md`, `.html`,
  `.htm` — OCR-free formats handled by `unstructured`). Any other format — `415`.
- A file whose text fully matches an already-loaded one is rejected — `400`. The match comes from
  the Redis registry, which is trusted only while Qdrant still backs it: when a registered name has
  no points left (the collection was re-created, the Qdrant instance replaced), the entry is treated
  as stale, dropped with a `stale_registry_entry_dropped` warning, and the upload proceeds normally.
- A file that could not be parsed — `422`.
- On success — `202` and a job identifier; processing runs in the background.

Response (`202`):

```json
{ "job_id": "1f0c...", "status": "queued" }
```

Example:

```
curl -X POST http://localhost:8000/documents \
     -F "file=@docs_data/docs_examples/СП_19.13330.2019_с_И1.docx"
```

## PATCH /documents/{name}

Delta update of a stored document under a new version. The body is the same multipart form as
`POST /documents` (minus `name`, which comes from the path).

Change detection is a deterministic diff of **source-block fingerprints**: at every ingest the
per-block content hashes of the source file are stored (Redis, `dvd:blocks:{name}:{version}`),
and each fragment records the source blocks it was built from (`src_block_ids`). The new file's
blocks are diffed against the latest stored version:

- a fragment whose source blocks are all **unchanged** is not re-indexed — the existing point
  just receives the new version in its multi-valued `versions` tag list;
- a fragment touching a **changed or added** block goes through the full pipeline (structure,
  tagging, embedding) and is inserted next to the shared ones, tagged with the new version only.

Because the source blocks are parsed deterministically, reuse does not depend on how the LLM
happened to split fragments this time. For versions stored before fingerprints existed (and for
synthetic fragments without source blocks) matching falls back to whitespace-normalized text
equality.

Both versions of the document live in the same structure (same `doc_id`); filtering search or
`GET /documents` by either version returns a complete document. The version comes from the
`version` form field, else the trailing 4-digit group of the name, else LLM detection; if the
resulting string already exists for this name, it gets a content-hash suffix (`… (ред. a1b2c3)`).

Unknown name — `404`; exact text duplicate — `400`; otherwise `202` + a job id (see
`GET /documents/{job_id}`, which also reports `new_nodes` / `reused_nodes`).

```
curl -X PATCH "http://localhost:8000/documents/СП%2019.13330.2019" \
     -F "file=@СП_19.13330.2019_ред2.docx" -F "version=2026"
```

## PUT /documents/{name}

Full reload (create-or-replace): every stored version of the document is deleted (Qdrant points +
Redis registry entries), then the file is ingested from scratch under the path name. No duplicate
rejection — re-uploading the same file is a legitimate way to rebuild the index. Returns `202` +
a job id.

```
curl -X PUT "http://localhost:8000/documents/СП%2019.13330.2019" \
     -F "file=@СП_19.13330.2019.docx"
```

## DELETE /documents/{name}

Delete a document from the store. Without parameters removes **all** versions; with
`?version=<v>` removes only that version: fragments belonging to it exclusively are deleted,
fragments shared with other versions only lose the version tag. Unknown name/version — `404`.

Response (`DeleteResponse`):

```json
{
  "name": "СП 19.13330.2019",
  "versions_removed": ["2026"],
  "points_deleted": 12,
  "points_updated": 254
}
```

```
curl -X DELETE "http://localhost:8000/documents/СП%2019.13330.2019?version=2026"
```

## POST /documents/direct

Ingest one or more documents **directly** from caller-supplied fragments, bypassing the LLM
structuring pipeline (`.docx` parsing → structure markup → hierarchy → tagging). The service only
embeds each fragment and upserts one Qdrant point per fragment. The result is a first-class
document — same collection, same payload schema, registered in the registry — so it is returned by
`GET /documents`, vector search, and `/library`, and can be removed with `DELETE /documents/{name}`.

The body is a JSON **array** of documents (a single document is an array of one). Only `name`
(document) and `text` (fragment) are required; everything else has a default. Fragments are stored
in array order — `order` is the array position and neighbours are linked (`prev_id`/`next_id`) so
the search context assembler works out of the box. `version` defaults to the trailing 4-digit group
of the name (e.g. `СП 2.13130.2020` → `2020`), else `"1"`. `embedding_provider` is reserved for
choosing the vectorizer later; if given it must match the configured provider (otherwise the
document's job fails).

One background job is queued **per document** (`202`); poll `GET /documents/{job_id}` for progress.
An exact-content duplicate is rejected per-document (`status="rejected"`, no `job_id`); the rest are
queued. Emits a `DirectDocumentProcessed` Kafka event per document (when Kafka is configured).

Request body:

```json
[
  {
    "name": "Регламент благоустройства",
    "version": "2026",
    "doc_type": "regulation",
    "corpus": "msi-tsim",
    "lang": "ru",
    "external_ids": {"code": "РБ-2026"},
    "metadata": {"source_system": "import"},
    "fragments": [
      {"text": "1 Общие положения", "type": "chapter", "numbering": "1"},
      {"text": "1.1 Настоящий регламент устанавливает…", "type": "clause", "numbering": "1.1", "tags": ["благоустройство"]}
    ]
  }
]
```

Response (`202`, one entry per document):

```json
[
  {"name": "Регламент благоустройства", "status": "queued", "job_id": "3f2b…", "error": null}
]
```

```
curl -X POST http://localhost:8000/documents/direct \
     -H "Content-Type: application/json" \
     -d '[{"name":"Регламент благоустройства","fragments":[{"text":"1 Общие положения"}]}]'
```

## PUT /documents/direct

Full replace (create-or-replace) of directly-ingested documents by name: every stored version of
each named document is wiped, then the supplied fragments are ingested from scratch. Same body and
response shape as `POST /documents/direct`, but **no duplicate rejection** — re-supplying the same
fragments is a legitimate way to rebuild the index. A document not stored yet is simply ingested.
Emits a single `DirectDocumentUpdated` when an existing document was replaced, or
`DirectDocumentProcessed` when the replace effectively created it.

```
curl -X PUT http://localhost:8000/documents/direct \
     -H "Content-Type: application/json" \
     -d '[{"name":"Регламент благоустройства","fragments":[{"text":"1 Общие положения (ред. 2)"}]}]'
```

## GET /documents

Documents already in the store, aggregated by `(name, version)` — one entry per document version,
not per fragment. Built by scrolling Qdrant and grouping fragment payloads; not served from the
Redis registry (which only tracks name/version existence, not these facts).

Query parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | null | filter by document name |
| `version` | str | null | filter by version |
| `block` | str | null | filter by `main`/`amendment` — keeps documents that have at least one node of this block |
| `tags` | list[str] (repeatable) | null | filter by tags (any of) |
| `uploaded_from` | str (ISO 8601) | null | only documents uploaded on/after this timestamp |
| `uploaded_to` | str (ISO 8601) | null | only documents uploaded on/before this timestamp |

`name`/`version`/`block`/`tags` are pushed down as Qdrant payload filters (all four fields are
indexed); `uploaded_from`/`uploaded_to` are applied afterwards, since upload time is a per-document
fact aggregated from fragments, not an indexed per-fragment field.

Response (`DocumentListResponse`):

```json
{
  "count": 1,
  "documents": [
    {
      "doc_id": "9f63...",
      "name": "СП 19.13330.2019",
      "version": "СП 19.13330.2019 (с Изменением N 1)",
      "other_versions": [],
      "blocks": ["amendment", "main"],
      "tags": ["зонирование", "противопожарные расстояния"],
      "node_count": 266,
      "uploaded_at": "2026-06-28T12:34:56.789012+00:00",
      "source": "СП_19.13330.2019_с_И1.docx"
    }
  ]
}
```

`blocks` and `tags` are the union across all fragments of that document version; `node_count` is
the number of fragments (texts and tables together). After a delta update
(`PATCH /documents/{name}`) a fragment may belong to several versions at once (multi-valued
`versions` tags) — it is counted under each of them, and the `version` filter matches the tags,
so every listed version is complete.

Examples:

```
curl "http://localhost:8000/documents"
curl "http://localhost:8000/documents?name=СП%2019.13330.2019"
curl "http://localhost:8000/documents?block=amendment&tags=зонирование&tags=здания"
curl "http://localhost:8000/documents?uploaded_from=2026-06-01T00:00:00%2B00:00"
```

## GET /documents/{job_id}

Background job status. The source is Redis.

Response:

```json
{
  "job_id": "1f0c...",
  "status": "done",
  "filename": "СП_19.13330.2019_с_И1.docx",
  "stage": "indexing",
  "stage_index": 7,
  "stage_total": 7,
  "task_progress": 100,
  "overall_progress": 100,
  "doc_id": "9f63...",
  "name": "СП 19.13330.2019",
  "version": "СП 19.13330.2019 (с Изменением N 1)",
  "other_versions": [],
  "nodes": 266,
  "error": null
}
```

Possible `status` values: `queued`, `processing`, `done`, `error`. If the job is not found — `404`.
`task_progress` is the normalized progress of the current stage; `overall_progress` is the
weighted end-to-end value. The server-side job starts at 10%, because the admin UI uses the first
10% for multipart file transfer. The same progress contract is used for upload, delta update and
full reload.

Indexing runs as a background task inside the application process and is **not resumed** after a
restart. Every job still marked `queued`/`processing` at startup is therefore flipped to `error`
("interrupted by a service restart"; logged as `orphaned_jobs_aborted`) and the scratch files in
`DVD_UPLOAD_DIR` are swept. An interrupted document has to be uploaded again — its MinIO original
and any Qdrant/Redis records are left untouched.

## Search

The `/search/texts`, `/search/tables` and `/search` endpoints take the same request body; they
differ only in the entity (`kind`) being searched: texts, tables or everything.

Request body (`SearchRequest`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | str | — | the search query |
| `name` | str | null | filter by document name |
| `document_names` | list[str] | null | filter results to documents matching any of these names |
| `version` | str | null | filter by version — matches the fragment's multi-valued `versions` tags, so fragments shared between versions after a delta update are found under each |
| `block` | str | null | filter by `main`/`amendment` |
| `types` | list[str] | null | filter by structural level (`chapter`/`clause`/`subclause`/...; any of) |
| `doc_id` | str | null | filter by a specific document |
| `doc_type` | str | null | filter by document type (`regulation`/`article`/…) |
| `corpus` | str | null | filter by logical corpus/namespace |
| `lang` | str | null | filter by language |
| `tags` | list[str] | null | filter by tags (any of) |
| `limit` | int | 10 | number of results |
| `context_height` | int | 0 | how many fragments before and after to attach |

Response (`SearchResponse`):

```json
{
  "count": 3,
  "hits": [
    {
      "id": "a1b2...",
      "score": 0.704,
      "doc_id": "9f63...",
      "name": "СП 19.13330.2019",
      "version": "СП 19.13330.2019 (с Изменением N 1)",
      "versions": ["СП 19.13330.2019 (с Изменением N 1)"],
      "other_versions": [],
      "kind": "text",
      "type": "clause",
      "block": "main",
      "numbering": "7.13",
      "breadcrumb": "СП 19.13330.2019 / 7 Инженерные сети / 7.13",
      "parent_id": "...",
      "prev_id": "...",
      "next_id": "...",
      "tags": ["противопожарные расстояния", "здания"],
      "references": [
        {
          "raw": "СП 42.13330.2016, п. 7.5",
          "target_name": "СП 42.13330.2016",
          "target_numbering": "7.5",
          "scope": "external",
          "target_doc_id": "c0ffee...",
          "target_version": "СП 42.13330.2016",
          "target_node_id": "b1ab1a...",
          "resolved": true
        }
      ],
      "text": "Расстояния от зданий и сооружений ...",
      "context": "... previous fragment ... target fragment ... next fragment ...",
      "table_html": null
    }
  ]
}
```

Results are sorted by descending relevance (`score` — cosine similarity). The `context` field is
filled only when `context_height > 0`. For tables, `table_html` is filled.

Besides the fields shown above, each hit also carries the general-purpose identity and grounding
fields from the payload: `title`, `version_id`, `doc_type`, `corpus`, `lang`, `external_ids`,
`order`, `metadata`, and the source span (`source_uri`, `char_start`, `char_end`, `page_start`,
`page_end`, `span_id`) — so a caller can cite the exact source location of every hit.

Examples:

```
curl -X POST http://localhost:8000/search/texts \
     -H "Content-Type: application/json" \
     -d '{"query": "санитарно-защитная зона", "limit": 3, "context_height": 1}'

curl -X POST http://localhost:8000/search/tables \
     -H "Content-Type: application/json" \
     -d '{"query": "расстояния между зданиями", "limit": 3}'

curl -X POST http://localhost:8000/search/texts \
     -H "Content-Type: application/json" \
     -d '{"query": "размещение предприятий", "version": "СП 19.13330.2019 (с Изменением N 1)", "tags": ["зонирование"]}'

curl -X POST http://localhost:8000/search/texts \
     -H "Content-Type: application/json" \
     -d '{"query": "расстояния", "block": "amendment", "types": ["clause", "subclause"]}'
```

Typing Cyrillic into `-d` from a Windows console may be mangled by the encoding; for manual checks
it is more convenient to use Swagger (`/docs`).

## GET /tags

All unique tags present in the document collection, sorted alphabetically. Built by scrolling
Qdrant payloads and unioning the `tags` field across every fragment — same source as the `tags`
values returned by `GET /documents` and search hits.

Response (`TagsResponse`):

```json
{
  "count": 2,
  "tags": ["зонирование", "противопожарные расстояния"]
}
```

Example:

```
curl "http://localhost:8000/tags"
```

## Administrative scope (level and territory)

Every document carries the administrative scope it applies to, derived from the Urban API territory
tree during ingestion (see `docs/en/pipeline.md`). It is a document-level fact: identical on every
fragment and shared by all versions of a document.

Fields returned on search hits, `GET /documents` and the library API:

| Field | Meaning |
|-------|---------|
| `document_level` | `federal` / `regional` / `municipal` — always derived from the territory's depth in the tree |
| `territory_id`, `territory_name` | the Urban API territory the document applies to (federal documents point at "Россия", 12639) |
| `territory_type_id`, `territory_type_name` | the Urban API territory type, for display |
| `territory_path` | ancestor ids, root first (`[12639, 1, 54]`) |
| `level_source`, `territory_source` | `manual` / `auto` / `unset` — automatic detection never overwrites `manual` |
| `territory_confidence` | automatic match confidence, `0..1` |
| `tagging_status`, `tagging_error` | `ok` / `pending`, and why the last attempt did not resolve |

Filters accepted by `POST /search*`, `GET /documents` and `GET /library/documents`:

| Parameter | Effect |
|-----------|--------|
| `document_level` | exact level match |
| `territory_ids` | a territory **and everything under it**, plus the higher-level documents **in force on it** — one OR over the stored ancestor chain |
| `tagging_status` | `pending` lists the documents still awaiting automatic tagging |

Setting a territory manually: `territory_id` as a form field on `POST`/`PATCH`/`PUT /documents` and
`/user-documents`, as a field of the direct-ingestion DTO, or via `PATCH
/library/documents/{doc_id}`. Sending `territory_id: null` in the PATCH clears the tag and hands
the document back to automatic detection. The level is never set directly — it follows the
territory, so the two cannot disagree.

| Method and path | Purpose |
|-----------------|---------|
| `GET /scopes` | levels and territories actually present in the collection, with document counts |
| `GET /tagging/pending` | documents whose scope is unresolved, with the reason for each |
| `POST /tagging/backfill` | run the tagging sweep now (`?dry_run=`, `?limit=`); returns a `job_id` |

```
curl "http://localhost:8000/scopes"
curl -X POST "http://localhost:8000/tagging/backfill?dry_run=true"
curl "http://localhost:8000/documents?document_level=municipal&territory_ids=54"
```

## Library (document read API)

A consumer-facing read API (e.g. for the MSI-TSIM service) that complements semantic search with
direct, per-document access: enumerate documents and fetch one by `doc_id` as assembled text +
metadata + ordered fragments, each with its source grounding.

### GET /library/documents

All documents registered in the store, from the Redis registry. Response (`DocumentList`):

```json
{
  "count": 1,
  "documents": [
    {
      "doc_id": "9f63...",
      "name": "СП 19.13330.2019",
      "title": null,
      "version": "СП 19.13330.2019 (с Изменением N 1)",
      "version_id": "сп_19_13330_2019__sha256_ab12cd34ef56",
      "other_versions": [],
      "doc_type": "regulation",
      "corpus": "norms",
      "lang": "ru",
      "status": "active",
      "external_ids": { "code": "СП 19.13330.2019" },
      "source_uri": "СП_19.13330.2019_с_И1.docx",
      "content_hash": "…",
      "node_count": 266,
      "uploaded_at": "2026-06-28T12:34:56.789012+00:00"
    }
  ]
}
```

### GET /library/lookup

Resolve documents by an exact lookup key / external id value (e.g. a normative code). Query
parameter `key` (required). Returns the same `DocumentList` shape.

```
curl "http://localhost:8000/library/lookup?key=СП%2019.13330.2019"
```

### GET /library/documents/{doc_id}

One document as `DocumentDetail` — the `DocumentSummary` fields above plus the assembled full `text`
(fragments joined in reading order) and the `fragments` array. If the document is not found — `404`.

Each fragment carries `id`, `order`, `kind`, `type`, `numbering`, `depth`, `breadcrumb`,
`parent_id`/`prev_id`/`next_id`/`child_ids`, the source grounding (`char_start`, `char_end`,
`page_start`, `page_end`, `span_id`), `tags`, `metadata`, `references` (outgoing links to other
documents/clauses — same shape as in search hits), `text` and `table_html`.

```json
{
  "doc_id": "9f63...",
  "name": "СП 19.13330.2019",
  "version": "СП 19.13330.2019 (с Изменением N 1)",
  "node_count": 266,
  "text": "… full document text in reading order …",
  "fragments": [
    {
      "id": "a1b2...",
      "order": 0,
      "kind": "text",
      "type": "title_page",
      "numbering": "",
      "char_start": 0,
      "char_end": 38,
      "span_id": "9f63...:span:0:38",
      "text": "СП 19.13330.2019 …"
    }
  ]
}
```

```
curl "http://localhost:8000/library/documents/9f63..."
```

## System

### GET /system/logs

Download the application log file as a readable `.log`. Optional query params `date` (`YYYY-MM-DD`)
and `request_id` narrow the output (combinable). `404` if the log file does not exist yet.

### GET /system/settings

Read the current effective configuration — the `DVD_` environment contract. Secrets
(`qdrant_api_key`) are returned masked as `***`. Each entry gives both the field name and the
env-var name, plus `restart_required` / `sensitive` flags. `vector_size` is the dimension actually
in use (auto-detected from the vectorizer at startup) — the quickest way to confirm Qdrant stores
2048-d vectors.

```json
{
  "effective_collection": "documents__giga_embeddings_instruct_2048",
  "registry_prefix": "dvd:documents__giga_embeddings_instruct_2048",
  "vector_size": 2048,
  "embeddings_provider": "giga",
  "env_file": ".env",
  "settings": [
    {"field": "search_limit", "env": "DVD_SEARCH_LIMIT", "value": 10, "restart_required": false, "sensitive": false},
    {"field": "vector_size", "env": "DVD_VECTOR_SIZE", "value": 2048, "restart_required": true, "sensitive": false},
    {"field": "qdrant_api_key", "env": "DVD_QDRANT_API_KEY", "value": "***", "restart_required": true, "sensitive": true}
  ]
}
```

### PUT /system/settings

Persist `DVD_` variables to the `.env` file and apply the runtime-tunable ones to the running app
immediately. Keys may be env names (`DVD_SEARCH_LIMIT`) or field names (`search_limit`); unknown
keys are rejected with `422`.

- **Runtime-tunable** (search/window/merge params, reference toggles, Ollama/vectorizer endpoints):
  applied in-memory at once — listed in `live_applied` — and effective on the next request/ingest.
- **Structural** (Qdrant collection & dimension, embeddings provider, Redis/Kafka wiring, logging,
  ingest concurrency): only written to `.env` — listed in `restart_required` — and effective after
  a restart. They are intentionally *not* mutated live, to avoid misrepresenting the running wiring
  (e.g. the Qdrant collection keeps its dimension until recreated).

```json
{
  "updates": {"DVD_SEARCH_LIMIT": 20, "DVD_SEMANTIC_MERGE_MAX_PASSES": 1}
}
```

Response:

```json
{
  "updated": [
    {"field": "search_limit", "env": "DVD_SEARCH_LIMIT", "value": 20, "restart_required": false, "sensitive": false},
    {"field": "semantic_merge_max_passes", "env": "DVD_SEMANTIC_MERGE_MAX_PASSES", "value": 1, "restart_required": false, "sensitive": false}
  ],
  "live_applied": ["search_limit", "semantic_merge_max_passes"],
  "restart_required": [],
  "restart_needed": false,
  "env_file": ".env"
}
```

```
curl -X PUT http://localhost:8000/system/settings \
  -H "Content-Type: application/json" \
  -d '{"updates": {"DVD_VECTOR_SIZE": 2048}}'
```

> The settings endpoints require a bearer service token. Keep them on a trusted network as well,
> since a write can change where the app points (Qdrant/Redis/Ollama) and toggle stages.

## MCP tools

The MCP server at `/mcp` (mounted via `FastMCP`) exposes the same capabilities as the REST API.
All tools are synchronous and share the same `Dependencies` singleton as the HTTP routers.

| Tool | Description |
|------|-------------|
| `search_texts` | vector search over text fragments; accepts the same filters as `POST /search/texts`, including `document_names` |
| `search_tables` | vector search over table fragments; same filters as `POST /search/tables` |
| `search_all` | vector search across all entities; same filters as `POST /search` |
| `list_documents` | list documents aggregated by `(name, version)` with optional filters |
| `job_status` | background job status by `job_id` |
| `document_versions` | list of versions already loaded for a document `name` |
| `pending_references` | dangling references awaiting a not-yet-loaded document `name` |
| `get_document` | a document by `doc_id`: assembled text + metadata + ordered fragments |
| `get_node` | one fragment plus its parent, children and neighbours — widen a hit without reading the whole document |

`search_*` also accept `parent_id` — search only inside one node (e.g. within a table you already found) instead of across the corpus.
Every `search_*` tool and `list_documents` also accept `document_level` and `territory_ids`; call `get_document_scopes` first — it is the only place those ids come from.
| `find_document` | resolve documents by lookup key / external id (`key`) |
| `get_tags` | all unique tags in the collection, sorted alphabetically — no parameters |
| `get_document_scopes` | levels and territories the collection actually holds, with document counts — where an agent gets `territory_ids` |

### get_tags

No parameters. Returns `TagsResponse`:

```json
{
  "count": 2,
  "tags": ["зонирование", "противопожарные расстояния"]
}
```
