<p align="center">
  <img src="docs/assets/logo.jpg" alt="DVD IDU" width="200">
</p>

<h1 align="center">DVD IDU</h1>

<p align="center">
  Preparation, structuring and vector indexing of regulatory documents
  with semantic search.
</p>

<p align="center">
  <b>English</b> · <a href="README-ru.md">Русский</a>
</p>

---

## Purpose

The application ingests a regulatory document (`.docx`), reconstructs its logical structure with
a large language model, splits it into meaningful fragments, vectorizes them and stores them in
Qdrant with rich metadata (structural element type, numbering, hierarchy, document version, tags,
links to neighbouring fragments). On top of the index it provides vector search over text and over
tables, with filters and context assembly of a configurable width.

The service tracks document versions, rejects exact duplicates, and stores tables as separate
entities.

## Features

- Parsing of `.docx` and reconstruction of the document's logical parts (stitching of broken
  paragraphs, isolation of clauses and sub-clauses).
- LLM structure markup: element type, own numbering, relative depth, amendment flag.
- Building the document hierarchy (sections, clauses, sub-clauses) and flattening it into nodes
  with links to parent, children and neighbouring fragments in reading order.
- Automatic detection of the document name and version.
- Fragment tagging.
- Vectorization with an embedding model and ingestion into Qdrant.
- Vector search over texts and over tables with filters (name, version, tags) and a context-width
  parameter.
- Full-text deduplication and versioning with a list of the document's other versions in the store.
- Background processing status stored in Redis.

## Requirements

- Python `>=3.11,<3.14`.
- [uv](https://docs.astral.sh/uv/) for dependency management.
- Qdrant (vector database).
- Redis (job statuses and the document/version registry).
- Ollama with two models:
  - an LLM for markup, merging, tagging and version detection;
  - an embedding model (default `bge-m3`, vector size 1024).
- Docker and Docker Compose — optional, to run the infrastructure and the app in containers.

OCR system libraries (poppler, tesseract) are not required for `.docx`: parsing goes through
`partition_docx` (python-docx), without heavy backends.

## Deployment

### 1. Infrastructure

```
docker compose up -d qdrant redis
```

Brings up Qdrant (`:6333`) and Redis (`:6379`) with data volumes.

### 2. Ollama models

The embedding model is mandatory; the LLM is your choice (see `docs/en/configuration.md`):

```
ollama pull bge-m3
ollama pull qwen2.5:7b-instruct
```

### 3. Configuration

Copy the example and adjust addresses and models if needed:

```
cp .env.example .env
```

Variables are prefixed with `DVD_`. `DVD_VECTOR_SIZE` must match the embedding model's dimension
(`bge-m3` = 1024). The full list is in `docs/en/configuration.md`.

### 4. Running the application

```
uv sync
uv run python -m src.dev_runner
```

The API docs (Swagger) are available at `http://localhost:8000/docs`.

### Running the application in Docker

The repository ships a `Dockerfile` and an `app` service in `docker-compose.yaml`:

```
docker compose up -d --build
```

The addresses of Qdrant, Redis and Ollama for the container are set via environment variables in
`docker-compose.yaml`.

## Documentation

- `docs/en/architecture.md` — architecture, modules and classes, data model.
- `docs/en/pipeline.md` — the document processing pipeline stage by stage, deduplication, versioning.
- `docs/en/api.md` — endpoints, request/response formats, examples.
- `docs/en/configuration.md` — environment variables and model recommendations.

Russian documentation: [`README-ru.md`](README-ru.md) and `docs/ru/`.

## Status

The core pipeline is implemented and verified end to end on a real regulatory document
(SP 19.13330.2019) with a local Ollama. Only the `.docx` format is supported; support for other
formats is planned.
