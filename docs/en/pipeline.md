# Document processing pipeline

Processing is started by `IngestionService.ingest(file_path, raw, content_hash, ...)` and runs in
the background after the upload request is answered. The input is already-extracted "raw" blocks and
the text hash (they are computed in the upload handler before the task is queued — this allows
rejecting a duplicate immediately, without launching the heavy parse).

## Stage 0. Text extraction

`DocumentParser.extract_raw(path)` parses `.docx` through `partition_docx` and returns a list of
blocks `{text, category, html}`. Service elements (headers/footers, page breaks) are dropped. For
tables, the HTML representation (`text_as_html`) is preserved.

`DocumentParser.content_hash(raw)` computes SHA-256 over the concatenated text of all blocks. This
hash is used for deduplication.

For `.docx`, the heavy unstructured backends (torch, OCR) are not engaged.

## Stage 1. Logical parts

The goal is to reconstruct coherent meaningful fragments even if the original formatting broke or
glued the text together.

1. Splitting: each block is split into atomic segments by inner list markers and numbering, and long
   blocks are additionally split on sentence boundaries. A dash is not treated as a marker (in
   regulatory texts it is usually punctuation), and tables are not split.
2. Boundary stitching: for each pair of adjacent segments it is decided whether this is a new part or
   a continuation of the previous one. Obvious cases are handled by a language-independent heuristic
   (punctuation, case, markers), ambiguous ones are passed to the LLM. Continuations are stitched.

## Stage 1.5. Semantic merge

The LLM merges parts that form a single semantic whole (continuation of a thought, an explanation,
an enumeration inside a clause, scattered service fragments of the title page and imprint). A part
that begins with its own structural number (e.g. `1.1`, `4.2`, `а)`, `1)`) is not merged into the
previous one — adjacent numbered clauses do not stick together.

The merge is iterative: passes repeat until convergence (by default at most two), because some merges
become apparent only after a previous merge.

## Stage 2. Structure markup

`StructureTagger.tag(parts, client)` returns four fields for each part:

- `type` — the kind of structural element by content (`chapter`, `section`, `clause`, `subclause`,
  `list_item`, `paragraph`, `table`, `note`, `definition`, `appendix`, `reference`, etc.; if nothing
  fits, the model forms its own short type);
- `numbering` — the part's own number, written out verbatim; codes and designations of other
  documents, law numbers and dates are not taken as the number;
- `relation` — depth relative to the previous part (`top`, `deeper`, `same`, `shallower`);
- `block` — `amendment` if the part belongs to a change/amendment, otherwise `main`.

After markup the own number is removed from the beginning of the text (kept separately in
`numbering`) to avoid duplication. The slice is protected against false matches.

## Stage 3. Categorization

Raw types are normalized: known synonyms are reduced to a common form (`глава` and `part` to
`chapter`, `содержание` to `toc`, etc.). Types produced by the model itself are kept as is.

## Stage 3.5. Numbering rank

`numbering_rank` determines depth by the number of components in a decimal number (`1` → 1,
`1.1` → 2, `4.2.1` → 3). This is a deterministic signal, independent of the LLM window. Codes and
years (a component of four or more digits) are not taken as a section number.

## Stage 4. Building the hierarchy

`HierarchyBuilder.build(parts, ranks, title)` assembles the tree. The depth of numbered parts is
taken from the numbering rank, that of the rest — from the relative `relation`. Nodes are arranged
with an ancestor stack; a child sits exactly one level deeper than its parent.

Post-processing:

- `cap_unnumbered_nesting` caps the depth of unnumbered nesting so that noisy `relation` does not
  produce degenerate chains; the overflow is flattened into a list.
- `group_amendment` collects consecutive amendment parts (`block=amendment`) into a separate
  container.

`flatten` unfolds the tree into a flat list of nodes in reading order and assigns each node a UUID,
the links `parent_id`, `parent_text`, `child_ids`, neighbours `prev_id`/`next_id`, `breadcrumb`, as
well as `kind` (`text`/`table`) and `table_html`.

## Stage 5. Version, tags, vectorization, ingestion

- `VersionDetector.detect(parts, client)` determines `name` (short designation) and `version` (full
  revision, including amendments).
- `Tagger.tag_nodes(nodes, client)` assigns each node tags — key topics and terms.
- An `uploaded_at` timestamp (current UTC time, ISO 8601) is set once for the whole batch and
  stamped onto every node of this ingest call — used by document listing (`GET /documents`) to
  show and filter by upload time.
- General-purpose identity is derived once: `version_id` (`<normalized name>__sha256_<12>`),
  `aliases`, `lookup_keys` (from `name` + any `external_ids`), plus the caller-supplied
  `doc_type` / `corpus` / `lang` / `title` / `metadata` (defaults from `Settings`).
- Source grounding is attached per node from the source elements it was built from (tracked as
  `src_ids` through Stages 1–4): `char_start` / `char_end` (offsets into the normalized source text
  from `DocumentParser.source_index`), `page_start` / `page_end` and `bbox` (when the format exposes
  them), and a derived `span_id`. `embedding_meta` records the vectorizer used.
- Node texts are vectorized by the embedding model (in batches).
- Points are ingested into Qdrant; the hash and version are registered in Redis, and a per-document
  summary is stored (`dvd:doc:{doc_id}`) for the document read API.
- When Kafka publishing is configured (`DVD_KAFKA_BOOTSTRAP_SERVERS`), a lifecycle event is queued
  to the Redis outbox and delivered to the `document.events` topic: `DocumentProcessed` for a first
  upload, `DocumentUpdated` for a delta update or full reload, `DocumentDeleted` for a deletion —
  so downstream services can react to every change of the stored corpus (see
  `docs/en/configuration.md`).

## Stage 5.5. Reference extraction and linking

Enabled by `enable_reference_linking` (default on). Runs after tagging, before vectorization.

- `ReferenceExtractor.extract(nodes, client)` asks the LLM (windowed, strict JSON — same shape as
  the structure/tagging stages) to pull out mentions of other documents: `raw` (verbatim, as
  written), `target_name` (the referenced designation) and `target_numbering` (the clause it points
  at, if any). Extraction is LLM-first by design.
- `ReferenceResolver.resolve(...)` turns each mention into a `DocumentRef` and resolves it against
  the store:
  - **internal** — a reference to the current document's own clause (no other designation): resolved
    against the freshly built `{numbering -> node_id}` index of the current document;
  - **external, target loaded** — matched against the registry of document names (normalized) and
    Qdrant; the exact clause becomes `target_node_id` (or, if only the document is found, a
    document-level link with `target_doc_id`);
  - **external, target missing** — left unresolved and pushed to the pending registry
    (`dvd:pending_ref:{normalized_name}` in Redis), keyed by the normalized designation.

Each reference is stored in two complementary forms: the human-readable `raw`/`target_name`/
`target_numbering`, and the machine `target_node_id`/`target_doc_id` that uniquely identify the
referenced part in the store.

After upsert and registration, `ReferenceResolver.backfill(name, ...)` drains the pending queue for
the just-ingested document and updates the source nodes' references in place — so a link written
before its target existed becomes resolved once that document arrives.

The regex seed (`reference_patterns.py`) and the durable learned-pattern collection in Qdrant are
the substrate for the optional self-improvement step gated by `ref_pattern_learning` (off by
default): the LLM generalizes new extraction patterns into the base over time.

## Deduplication

Before queuing the background task, the upload handler extracts the text and computes
`content_hash`. If such a hash is already registered, the upload is rejected with code 400 — the
text fully matches an already-loaded document.

## Versioning

If the text differs, the document is loaded as a new version. The document name (`name`) identifies
the logical document under which versions are tracked. On upload:

- the `other_versions` field of the new nodes records the document's other versions already present
  in the store;
- the `other_versions` field on points of previously loaded versions is updated to include the new
  version;
- if the version string matched an existing one but the text differs, the version is made
  distinguishable by appending a short hash suffix.

## Tables

Tables are stored as separate entities: nodes with `kind=table` containing `table_html`. They are
not merged with surrounding text and are available through a dedicated search endpoint.

## Neighbouring fragments and context width

The `prev_id` and `next_id` fields define the document's reading order. On search, the
`context_height` parameter specifies how many fragments before and after the match to attach: the
service walks the `prev`/`next` chain for the given number of steps and assembles the expanded text.
This allows obtaining either a pinpoint fragment or a wider context around it.

## Windows and reconciliation

Lists of parts are split into overlapping windows (`make_windows`) by a character budget and a limit
on the number of items (long arrays degrade the model's structured output). Decisions on overlapping
items are reconciled (`reconcile`) with priority to the window where an item has more left context.

## Notes on quality and speed

- Speed is determined by the LLM and the hardware. Windows are processed sequentially, so a large
  document takes significant time; the main headroom for speedup is parallel window processing and a
  more performant feed into the model.
- Structure markup quality depends on the model. The document's backbone (sections, clauses,
  numbering, version, tables) is extracted robustly; service fragments and reference lists may be
  marked up more coarsely.
