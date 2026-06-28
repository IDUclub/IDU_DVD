# API

The default base URL is `http://localhost:8000`. Interactive docs (Swagger) are at `/docs`. All
request and response models are pydantic-based and defined under `src/dvd_service/dto/`.

## Endpoint list

| Method and path | Purpose |
|-----------------|---------|
| `POST /documents` | upload a `.docx` document and queue it for processing |
| `GET /documents` | list ingested documents, aggregated by (name, version), with filters |
| `GET /documents/{job_id}` | processing job status |
| `POST /search/texts` | search relevant text fragments |
| `POST /search/tables` | search relevant tables |
| `POST /search` | search across all entities (texts and tables) |
| `GET /ping` | health check |
| `GET /` | redirect to `/docs` |

## POST /documents

Upload a document. The body is a multipart form.

Form fields:

- `file` — a `.docx` file (required);
- `version` — a version string to override auto-detection (optional).

Behaviour:

- Only `.docx` is accepted. Any other format — `415`.
- A file whose text fully matches an already-loaded one is rejected — `400`.
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
the number of fragments (texts and tables together).

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
  "doc_id": "9f63...",
  "name": "СП 19.13330.2019",
  "version": "СП 19.13330.2019 (с Изменением N 1)",
  "other_versions": [],
  "nodes": 266,
  "error": null
}
```

Possible `status` values: `queued`, `processing`, `done`, `error`. If the job is not found — `404`.

## Search

The `/search/texts`, `/search/tables` and `/search` endpoints take the same request body; they
differ only in the entity (`kind`) being searched: texts, tables or everything.

Request body (`SearchRequest`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | str | — | the search query |
| `name` | str | null | filter by document name |
| `version` | str | null | filter by version |
| `block` | str | null | filter by `main`/`amendment` |
| `types` | list[str] | null | filter by structural level (`chapter`/`clause`/`subclause`/...; any of) |
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
