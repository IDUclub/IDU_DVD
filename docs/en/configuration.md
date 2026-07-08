# Configuration

Settings are defined by the `Settings` class (`src/common/config/app_config.py`) on top of
pydantic-settings. Values are overridden by environment variables with the `DVD_` prefix or via a
`.env` file. A variable name is `DVD_` plus the field name in upper case (for example, the
`vector_size` field corresponds to `DVD_VECTOR_SIZE`).

List fields (`languages`, `allowed_extensions`) are set in the environment in JSON format, e.g.
`DVD_ALLOWED_EXTENSIONS='[".docx",".txt",".md"]'`.

Every field has a default in code, so the application starts without a `.env` at all. Two example
files ship with the repo: **`.env.example`** — the service's network links (local
Ollama/Qdrant/Redis addresses, the Kafka switch); **`.env.full.example`** — the full reference
mirroring the defaults. This page is the canonical variable list: when adding a setting, update
the table here and `.env.full.example` together.

## Variables

### Ollama

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_OLLAMA_BASE` | `http://a.dgx:11434` | Ollama address |
| `DVD_OLLAMA_MODEL` | `gpt-oss:20b` | LLM for markup, merge, tags, version |
| `DVD_OLLAMA_EMBED_MODEL` | `bge-m3` | embedding model for `DVD_EMBEDDINGS_PROVIDER=ollama` |
| `DVD_OLLAMA_NUM_CTX` | `16384` | model context size |
| `DVD_OLLAMA_NUM_PREDICT` | `8192` | response token cap |
| `DVD_OLLAMA_TIMEOUT` | `600.0` | request timeout, seconds |

### Embeddings provider (vectorizer)

The default vectorizer is the **giga-vectorizer** GPU service (a separate repository) exposing an
OpenAI-compatible `POST /v1/embeddings` with a `prompt` extension: documents are embedded without
an instruction prefix, queries with one (Giga-Embeddings-instruct is asymmetric). Ollama (`bge-m3`)
remains available as a fallback provider for GPU-less environments.

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_EMBEDDINGS_PROVIDER` | `giga` | `giga` — GPU service (2048-d) / `ollama` — fallback via `/api/embed` (1024-d) |
| `DVD_EMBEDDINGS_URL` | `http://localhost:8001` | giga-vectorizer address |
| `DVD_EMBEDDINGS_MODEL` | `ai-sage/Giga-Embeddings-instruct` | model served by the vectorizer (checked by the service) |
| `DVD_EMBEDDINGS_QUERY_PROMPT` | `Instruct: Дан вопрос, необходимо найти абзац текста с ответом\nQuery: ` | instruction prefix for query embeddings |
| `DVD_EMBEDDINGS_TIMEOUT` | `600.0` | request timeout, seconds |

Switching providers changes the vector space: adjust `DVD_VECTOR_SIZE`. With collection namespacing
enabled, the service will use a new physical Qdrant collection and you need to re-ingest documents
into that new space (embeddings of different models are not comparable).

### Qdrant

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_QDRANT_URL` | `http://localhost:6333` | Qdrant address |
| `DVD_QDRANT_API_KEY` | empty | API key (if required) |
| `DVD_QDRANT_COLLECTION` | `documents` | **base** collection name (see Collection namespacing) |
| `DVD_VECTOR_SIZE` | `2048` | vector dimension; must match the embeddings provider model (giga = 2048, bge-m3 = 1024) |
| `DVD_EMBED_BATCH` | `32` | batch size during vectorization |
| `DVD_COLLECTION_NAMESPACING` | `true` | derive a distinct physical collection per embedding space (see below) |

#### Collection namespacing

With `DVD_COLLECTION_NAMESPACING=true` (default) the **physical** Qdrant collection is not
`DVD_QDRANT_COLLECTION` verbatim — it is derived from the base name, the embedding model and the
dimension:

```
{base}__{model_slug}_{dim}
documents__giga_embeddings_instruct_2048     # giga / 2048
documents__bge_m3_1024                        # ollama fallback / 1024
```

At startup the service creates the collection its **current** config points at, if it does not
exist yet. Because the name encodes the embedding space, changing the provider/model/dimension
lands in a **brand-new** collection and the previous one is left untouched — a provider switch (or
a rollback) never overwrites or silently mixes vector spaces, and no manual "drop + re-index"
dance is needed. You still re-ingest documents into the new space (embeddings of different models
are not comparable), but the old space stays available.

The Redis document registry (dedup hashes, version sets, document summaries) is namespaced the
same way (`dvd:{effective_collection}:…`), so a fresh space also gets a clean registry — otherwise
the duplicate check would wrongly reject re-ingesting documents that only exist in the *old* space.

Set `DVD_COLLECTION_NAMESPACING=false` to use `DVD_QDRANT_COLLECTION` verbatim (legacy behaviour,
legacy `dvd:` registry prefix). In that mode, if the existing collection's dimension does not match
`DVD_VECTOR_SIZE`, startup **fails fast** instead of writing mismatched vectors. The learned
reference-pattern collection (`DVD_REF_PATTERN_COLLECTION`) is model-independent and is never
namespaced.

### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_REDIS_URL` | `redis://localhost:6379/0` | Redis address |
| `DVD_REDIS_JOB_TTL` | `86400` | job status TTL, seconds |

### Kafka (document lifecycle events)

Publishing is optional and stays off until `DVD_KAFKA_BOOTSTRAP_SERVERS` is set. Document
lifecycle changes are appended to a durable Redis outbox and delivered to the `document.events`
topic by a background publisher (the [otteroad](https://github.com/IDUclub/otteroad) framework:
AVRO + Schema Registry). Events that keep failing are moved to a dead-letter list instead of
blocking the queue. Event types:

| Event | When | Payload |
|-------|------|---------|
| `DocumentProcessed` | first upload of a document (`POST /documents`) | `document_name` |
| `DocumentUpdated` | delta update or full reload (`PATCH`/`PUT /documents/{name}`) | `document_name`, `version` |
| `DocumentDeleted` | deletion of a document or one version (`DELETE /documents/{name}`) | `document_name`, `versions_removed`, `document_removed` |

A `PUT` reload announces a single `DocumentUpdated` (no intermediate `DocumentDeleted`); a reload
of a not-yet-stored document announces `DocumentProcessed`.

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_KAFKA_BOOTSTRAP_SERVERS` | empty | Kafka brokers (`host:port[,host:port]`); empty = publishing disabled |
| `DVD_KAFKA_SCHEMA_REGISTRY_URL` | `https://schema-registry.next.idulab.ru` | AVRO Schema Registry address (default = the IDU contour registry) |
| `DVD_KAFKA_CLIENT_ID` | `idu-dvd` | client id shown in broker logs |
| `DVD_KAFKA_OUTBOX_KEY` | `dvd:kafka:outbox` | Redis list with pending events |
| `DVD_KAFKA_DEAD_LETTER_KEY` | `dvd:kafka:outbox:dead` | Redis list for events that exhausted retries |
| `DVD_KAFKA_POLL_INTERVAL` | `1.0` | seconds between outbox checks when idle |
| `DVD_KAFKA_RETRY_INTERVAL` | `5.0` | seconds to wait after a failed send |
| `DVD_KAFKA_MAX_ATTEMPTS` | `10` | send attempts before an event is dead-lettered |

### Search

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_SEARCH_LIMIT` | `10` | default number of results |
| `DVD_MAX_CONTEXT_HEIGHT` | `6` | cap on context width (neighbours before and after) |

### Reference linking

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_ENABLE_REFERENCE_LINKING` | `true` | extract and resolve links to other documents/clauses |
| `DVD_REF_PATTERN_LEARNING` | `false` | let the LLM grow the regex pattern base (self-improvement) |
| `DVD_REF_PATTERN_COLLECTION` | `ref_patterns` | Qdrant collection for learned patterns |

### Pipeline

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_PARTITION_STRATEGY` | `hi_res` | unstructured strategy (for formats other than `.docx`) |
| `DVD_LANGUAGES` | `["rus","eng"]` | languages for parsing |
| `DVD_WINDOW_CHARS` | `6000` | character budget per window |
| `DVD_WINDOW_MAX_ITEMS` | `22` | item limit per structure-markup window |
| `DVD_OVERLAP_BLOCKS` | `3` | window overlap |
| `DVD_SEMANTIC_MERGE_MAX_PASSES` | `2` | number of semantic-merge passes |
| `DVD_SPLIT_SENTENCES` | `true` | split long blocks into sentences |
| `DVD_SENT_MIN_LEN` | `300` | minimum block length to split into sentences |

### Upload

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_INGEST_CONCURRENCY` | `1` | how many documents may run the GPU-bound pipeline (LLM markup/tags/refs + embeddings) at once; extra documents wait in status `queued`. Keep `1` for a single GPU; raise only with more GPU capacity |
| `DVD_UPLOAD_DIR` | `./_uploads` | directory for temporary upload files |
| `DVD_ALLOWED_EXTENSIONS` | `[".docx",".txt",".md",".html",".htm"]` | allowed extensions (OCR-free formats handled by `unstructured`; scanned PDF/OCR is deferred — add `".pdf"` once the heavy backends are provisioned) |

### Document identity defaults

Generic fallbacks for the cross-service payload fields when the uploader omits them; domain
consumers (e.g. MSI-TSIM) override these per upload via form fields / `external_ids`.

| Variable | Default | Description |
|----------|---------|-------------|
| `DVD_DEFAULT_DOC_TYPE` | `document` | default `doc_type` (`document`/`regulation`/`article`/`book`/`web`/…) |
| `DVD_DEFAULT_CORPUS` | `default` | default logical corpus/namespace |
| `DVD_DEFAULT_LANG` | empty | default ISO-639 language code (none = unknown) |

## Important notes

- `DVD_VECTOR_SIZE` must match the dimension of the chosen embeddings provider model:
  Giga-Embeddings-instruct — 2048, `bge-m3` — 1024. The dimension is fixed when the Qdrant
  collection is created and cannot be changed without recreating it. With collection namespacing on
  (default) a dimension change simply provisions a new collection; with it off, re-index the
  existing collection (or switch it on).
- `DVD_PARTITION_STRATEGY` affects only the parsing of formats other than `.docx`; `.docx` is parsed
  through `partition_docx` regardless of the strategy.
- By default the Ollama address and the LLM point at a shared stand (`a.dgx`, `gpt-oss:20b`). For a
  local run, override them in `.env`.

## Model recommendations

### CPU-only (CI, no GPU)

- LLM: `qwen2.5:3b-instruct` — small enough for CPU inference; the integration suite passes with
  it (structured output is grammar-constrained by Ollama, so JSON validity does not depend on model
  size). Used by the Integration workflow in CI. Expect markup quality below the 7B+ models — fine
  for tests, not recommended for real corpora.
- Embeddings: the `ollama` provider with `bge-m3` (CPU-friendly as is); giga-vectorizer is
  CUDA-only and fails to start without a GPU.

```
DVD_EMBEDDINGS_PROVIDER=ollama
DVD_OLLAMA_EMBED_MODEL=bge-m3
DVD_VECTOR_SIZE=1024
```

### Local run on a modest GPU

- LLM: `qwen2.5:7b-instruct` (good Russian and stable structured output, fits in 8 GB of VRAM).
- Embeddings: giga-vectorizer (Giga-Embeddings-instruct, 2048) — the default; the model is tight on
  8 GB cards, so on a single small GPU shared with the LLM prefer the `ollama` fallback
  (`bge-m3`, 1024).

```
DVD_OLLAMA_BASE=http://localhost:11434
DVD_OLLAMA_MODEL=qwen2.5:7b-instruct
DVD_EMBEDDINGS_URL=http://localhost:8001
```

### A server with several Tesla V100s

Tesla V100 (Volta architecture) works reliably in FP16; fast int4 kernels (AWQ, GPTQ-marlin), FP8
and Flash-Attention 2 are not supported. Use FP16 (when moving to vLLM) or GGUF quantization (when
working through Ollama).

- LLM: `qwen2.5:14b-instruct` as a working option (noticeably better than 7B, fits with room to
  spare), or `qwen2.5:32b-instruct` for higher quality if 32 GB cards are available.
- Embeddings: giga-vectorizer (Giga-Embeddings-instruct, FP16 — Volta-friendly) on a dedicated GPU.
- Layout across three cards: the LLM with tensor parallelism on two GPUs (for 14B a parallelism of 2
  is acceptable; parallelism of 3 is not used because the number of heads is not divisible), the
  embeddings on the third GPU.
- The main speedup reserve is parallel processing of pipeline windows and batching of requests to the
  model (via vLLM, or via `OLLAMA_NUM_PARALLEL` and concurrent requests).
