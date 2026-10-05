# Document processing pipeline

Processing is started by `IngestionService.ingest(file_path, raw, content_hash, ...)`, called by an
ingestion worker that claimed the job from the queue — possibly in a different process than the one
that answered the upload. The input is already-extracted "raw" blocks and the text hash: the hash is
computed in the upload handler so a duplicate is rejected immediately, without launching the heavy
parse, and the blocks are re-extracted by the worker from the original stored in MinIO.

> **Direct ingestion bypasses this pipeline.** `POST`/`PUT /documents/direct`
> (`IngestionService.ingest_direct` / `reload_direct`) take caller-supplied fragments and run only
> the last two steps below — embedding and indexing — with no `.docx` parsing, structure markup,
> hierarchy, tagging, or reference stages. `order`/`prev_id`/`next_id` follow the array order.
> See `docs/en/api.md`.

## Stage 0. Text extraction

`DocumentParser.extract_raw(path)` reads `.docx` through `DocxReader` (python-docx/OOXML).
Automatic Word labels are materialized before hashing or LLM processing, including inherited
paragraph styles, nested counters, start overrides and restarts. Paragraphs and tables retain
reading order; merged cells are represented in table HTML. Headers and footers are excluded.
Unsupported numbering definitions raise an explicit error rather than inventing a label.
Other formats retain the unstructured extraction path.

`DocumentParser.content_hash(raw)` computes SHA-256 over the concatenated text of all blocks. This
hash is used for deduplication.

For `.docx`, the heavy unstructured backends (torch, OCR) are not engaged.

**PDF.** `PdfReader` reads a PDF page by page. A page whose text layer has at least
`DVD_OCR_MIN_TEXT_CHARS` characters is taken as is, one block per line. Any other page is a scan:
it is rendered at `DVD_OCR_DPI` (at most `DVD_OCR_MAX_PIXELS`) and sent to the OCR service (`DotsOcrClient`, dots.ocr behind an
OpenAI-compatible vLLM, one page per request, `DVD_OCR_CONCURRENCY` in parallel). Its layout
elements become blocks: titles and section headers → `Title`, list items → `ListItem`, text,
captions, footnotes and formulas → `NarrativeText`, tables keep their HTML (`Table`); running
headers, footers and pictures are dropped. Every block carries its `page` and `bbox`. Recognized
pages are cached under `DVD_OCR_CACHE_DIR` by the file hash, so a retried job does not OCR again;
the job shows the `ocr` stage with page progress. A scanned PDF is not recognized in the upload
request: its duplicate check uses the hash of the file bytes. Without `DVD_OCR_BASE_URL` a PDF
with scanned pages is refused with `422`; a PDF with a text layer is still read.

## Alternative Stage 1: validated ID ranges

`DVD_LOGICAL_PARTITION_MODE=ranges` selects the experimental source-preserving path. The default
`boundaries` keeps Stages 1 and 1.5 described below. The local `document_parsing_pipeline.ipynb`
selects `ranges` explicitly so the two approaches can be compared before changing the service.

1. `DocumentParser.source_units` creates paragraph/sentence units with stable IDs and exact
   character offsets into `source_index(raw)`. Explicit numbered provisions, headings, notes and
   tables are kept intact. Sentence splitting uses `split_sentences` and `sent_min_len`.
2. `prepare_range_units` protects provision, subpoint, heading and table boundaries.
   Numbered leaves remain separate until their types and parentage are known.
3. `RangePartitioner.windows` packs units without overlap, using `window_chars` and
   `window_max_items`. An indivisible unit larger than the input budget is sent alone in full.
4. The LLM returns only inclusive `{start_id, end_id}` ranges. Python requires ordered, complete,
   non-overlapping coverage and valid integer IDs. Valid partitions are cut deterministically at
   protected boundaries and the 512-character merge limit. Boundary violations require no retry;
   invalid IDs/coverage and transient request failures retry up to three attempts, then fail closed.
5. Python slices the source. No LLM-generated text is accepted. The old semantic merge is skipped;
   `overlap_blocks` and `semantic_merge_max_passes` do not affect this stage. Later structure and
   reference stages retain their existing window settings.
6. After type markup, adjacent `title_page` lines become one cover leaf, exempt from the 512 limit.
   Cover detection depends on the LLM; contents, prefaces and body parts are not included.
7. Hierarchy uses explicit headings and number prefixes (`1.1` belongs to `1`). Explicit legal
   articles retain their numbering semantics: `3` and `3.3` are sibling provisions; `1)` and `а)`
   form nested lists. Final assembly proceeds from parents down:
   - A provision and its entire subtree at **512 characters or less** become one content leaf.
   - A larger provision retains its own text as a structural container (`is_container=true`).
   - If all its subpoints together fit, they become one child `subclause_group`.
   - Otherwise each subpoint is examined separately using the same rule.
   - Colon-introduced lists follow the same rule. Tables, distinct amendment blocks and gaps in
     source spans prevent joins.

Size uses `source_text`, including numbers, spaces and line breaks. An indivisible oversized source
leaf remains intact: 512 limits merging, not arbitrary text cutting. `source_text` preserves the
exact extracted slice through hierarchy, Qdrant and `/library`; `char_start`/`char_end` address that
slice. Display `text` can omit its own number, but retains absorbed subpoint numbering.

Containers remain in the tree and library; semantic search returns content nodes. Embeddings use
`search_text`, combining content with full structural ancestor context. This separate context is
not counted against the 512-character content limit or its source span. Search hits and library
fragments expose `search_text` as well.

**Documents converted from PDF.** When the extracted text carries a *running header* — a document
designation followed by a page number («СП 42.13330.2026 85») repeated at least five times —
`SourceLayout` re-cuts the units before the LLM sees them, because such a conversion follows the
page layout: one paragraph can hold the tail of a clause, a section heading and the next clauses,
and the header often sits right in front of a clause number.

- Each running header joins the unit before it. It stays in `source_text` (the exact coverage of
  the source is unchanged) but is removed from the fragment `text`, which also joins letter-spaced
  words («Т а б л и ц а» → «Таблица»).
- The column head a multi-page table repeats under the running header («Объекты1) Нормативная
  потребность1) …») is treated as part of the header when at least two page tops share 40+
  characters of it, so it does not land in the middle of a row. A repeat that opens with an
  appendix heading, a caption or a clause address is structure and is kept.
- A range abbreviation («св. 30 до 170») does not end a sentence.
- Clause addresses inside a paragraph become unit starts only when they belong to the document's
  *address run*: the longest chain of addresses where each follows the previous one (a first
  child `6.1.1`, the next number at some level, up to two lost numbers tolerated). References
  («согласно 6.1.11»), table-of-contents entries and codes in flattened tables form short chains
  of their own and do not cut anything. Table captions («Таблица 6.8 –») and notes also start units.
- A leading decimal number that is not in the run (a note item «2 При определении …», a code) is
  not an address; a top-level number in the run opens a section, titled when the unit is only the
  heading.
- The hierarchy finds a clause's parent by its address when an unnumbered heading-like part (a
  table row the model took for a title) has reset the stack.

Documents without running headers are cut exactly as before.

Fidelity is relative to extracted normalized source text, not DOCX bytes or page layout. The parser
version suffix is `-semantic1-ranges`. Range-mode updates rebuild a complete revision and embeddings:
source-block reuse could retain obsolete grouping or parent context. Legacy `boundaries` updates
keep their reuse strategy. Retrieval quality and cover classification still require evaluation with
a real model; deterministic fake-LLM tests verify source fidelity and assembly rules only.

## Stage 1. Logical parts

The goal is to reconstruct coherent meaningful fragments even if the original formatting broke or
glued the text together.

1. Splitting: Word paragraphs and tables retain their source boundaries. Other blocks may be split
   at line-start list markers and sentence boundaries. Inline references and dates never start
   new clauses. Explicit numbered provisions remain intact.
2. Boundary stitching: for each pair of adjacent segments it is decided whether this is a new part or
   a continuation of the previous one. Obvious cases are handled by a language-independent heuristic
   (punctuation, case, markers), ambiguous ones are passed to the LLM. Continuations are stitched.

Before applying pairwise decisions, the parser measures complete structural groups against a
**512-character limit** (`STRUCTURAL_GROUP_MAX_CHARS`):

- An introduction ending in `:` and its consecutive marked list items form a group.
- A numbered provision and its descendants form a group even without a colon: `1` → `1.1` →
  `1.1.1`. Sibling provisions are not combined. Inside explicit articles, decimal parts such as
  `3` and `3.3` remain siblings; parenthesized numeric and letter items remain nested.
- The check runs from parent to children. If the entire subtree fits (≤512), it becomes one
  fragment. Otherwise the parent's own text stays separate and each child subtree is checked
  independently, descending as far as necessary. For a flat oversized list, every item starts
  a new fragment; no short prefix of that list is merged.

The count includes the introduction/parent text, all descendants, their markers and the spaces
inserted when joining them. It counts characters, not bytes or tokens. A leaf that already exceeds
512 characters stays intact: this is a grouping threshold, not a hard limit on fragment length.
Groups use explicit source markers; tables, headings, editorial notes and unmarked prose end a
contiguous run. These decisions override the boundary LLM and are preserved through semantic merge.

## Stage 1.5. Semantic merge

The LLM merges parts that form a single semantic whole (continuation of a thought, an explanation,
an enumeration inside a clause, scattered service fragments of the title page and imprint). A part
that begins with its own structural number (e.g. `1.1`, `4.2`, `а)`, `1)`) is not merged into the
previous one — adjacent numbered clauses do not stick together. Explicit article/chapter headings
and editorial notes are also protected boundaries.

The merge is iterative: passes repeat until convergence (`DVD_SEMANTIC_MERGE_MAX_PASSES`, default 1),
because some merges become apparent only after a previous merge. Raise the cap to trade extra LLM
passes for more aggressive merging.

## Stage 2. Structure markup

`StructureTagger.tag(parts, client)` returns five fields for each part in a single LLM pass:

- `type` — the kind of structural element by content (`chapter`, `section`, `clause`, `subclause`,
  `list_item`, `paragraph`, `table`, `note`, `definition`, `appendix`, `reference`, etc.; if nothing
  fits, the model forms its own short type);
- `numbering` — the part's own number, written out verbatim; codes and designations of other
  documents, law numbers and dates are not taken as the number;
- `relation` — depth relative to the previous part (`top`, `deeper`, `same`, `shallower`);
- `block` — `amendment` if the part belongs to a change/amendment, otherwise `main`;
- `tags` — 2–6 search tags (key topics/objects/terms), lowercase, in the part's language; empty for
  boilerplate parts. Folding tags into this pass removes what used to be a separate tagging LLM pass
  over every node; the tags are carried through Stage 4 onto the flat nodes.

Explicit source headings/numbers override conflicting LLM markup. Editorial notes carry no own
number; an LLM number absent from the start of the source is discarded.

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
taken from the numbering rank, that of the rest — from the relative `relation`. Inside explicit
articles, inserted parts such as `3.3` are siblings of part `3`; parenthesized list items remain
below the current provision. Chapters, sections and articles anchor the outer hierarchy. Nodes are arranged
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

- `VersionDetector.detect_head(parts, client)` reads the document head (title page, foreword,
  imprint) once and returns five fields: `name` (short designation), `version` (full revision,
  including amendments), and the administrative-scope hints `level` / `territory` / `region`. It is
  one structured-output call, not two — the head is where both facts are stated. When name and
  version are supplied manually *and* a `territory_id` was given, the call is skipped entirely.
- The scope hints are resolved against the Urban API territory tree (`TerritoryResolver`) — see
  "Administrative scope" below.
- Fragment tags (key topics and terms) are **not** a separate LLM pass: `StructureTagger.tag` emits
  them alongside the structural fields in Stage 2, and they ride the hierarchy onto each node, so
  Stage 5 just reads `node["tags"]`.
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

## Administrative scope (document level and territory)

Every fragment carries the document's administrative scope, so a caller can filter by it and cite
it: `document_level` (`federal` / `regional` / `municipal`), `territory_id` / `territory_name` /
`territory_type_id` / `territory_type_name`, and `territory_path` — the territory's ancestor chain,
root first (`[12639, 1, 54]`). It is a document-level fact: identical on every fragment and shared
by every version of a document (they share one `doc_id`).

**The level is always derived from the territory**, from its depth in the Urban API tree (1 =
"Россия" → federal, 2 = subject of the federation → regional, deeper → municipal), never from
`territory_type`: a type does not determine depth ("Город" is a level-3 municipality in one place
and a settlement inside one in another), and "Город федерального значения" (Moscow, Saint
Petersburg) is a *region* despite its name. Federal documents point at "Россия" (12639), so a level
without a territory does not exist.

Resolution order:

1. an explicit `territory_id` from the request (upload form, direct-ingestion DTO, `PATCH
   /library/documents/{doc_id}`) — recorded as `manual`;
2. a `manual` territory already stored on the document — carried over on a new version and on a
   full reload (a reload rescues it before wiping the document, so `PUT` cannot silently undo an
   admin's work);
3. automatic matching from the head hints: `federal` → "Россия"; `regional` → matched against the
   89 subjects; `municipal` → found through the Urban API's server-side name search and narrowed by
   the region hint.

If no lower-level territory matches, the head pass also checks the document's own federal
scope. It returns `federal_scope_evidence`, a supporting quotation that must occur in the
source fragments. With this evidence, the resolver may fall back to "Россия" (12639),
including when the initial level was unknown. An explicit country hint ("Россия",
"Российская Федерация", "РФ") also resolves to Russia. A matched region or municipality
takes precedence over the evidence-based fallback. Merely failing to find a territory,
citing a federal act, or mentioning Russia in an address is not sufficient.

**An ambiguous name is not resolved.** "Кировский район" exists in a dozen regions; the document is
stored with `tagging_status="pending"` and the reason in `tagging_error` instead of a plausible
guess. The same happens when the Urban API is unreachable — ingestion is never blocked by it.

`territory_source` / `level_source` (`manual` / `auto` / `unset`) and `territory_confidence` record
where the values came from. **Automatic detection never overwrites `manual`**; an explicit
`territory_id` may override an earlier one (a human may override a human).

Pending documents are picked up by `TaggingBackfillService`: ~30 s after startup, then hourly, and
on demand via `POST /tagging/backfill`. It re-runs the head pass over the stored fragment text (no
source file needed), completes a manually chosen territory without reconsidering it, and gives up
on a document after `DVD_TAGGING_MAX_ATTEMPTS`, recording the reason.

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

## Stage 6.5. Fragment relations

Enabled by `enable_relations` (default on). Runs after vectorization, before indexing.

Structure says where a fragment sits; a **relation** says which other fragments its meaning needs.
A relation is directed: `source` depends on `target` with a `weight` in 0..1 (to understand or apply
the source you have to read the target) and a `kind` — `completes` (the rest of its sentence, the
items after its lead-in), `condition`, `exception`, `refines` (values, details), `table_ref`,
`definition`, `same_topic`. Both directions of a pair are scored separately: a lead-in «следует
учитывать:» depends on its items much more than an item depends on it.

Scoring every pair is quadratic (SP 42 has ~1.5M pairs), so `RelationCandidates` proposes pairs
first (~13.5k for SP 42):

- **structure** — parent/child, grandparent/grandchild, siblings (all pairs in groups up to
  `relation_sibling_full`, a ±`relation_sibling_window` window in larger ones), and neighbours in
  reading order that break a phrase between them — a table flattened by a PDF conversion is cut
  mid-row («… мест: св.» / «30 до 170 включительно – 80 м2 на 1 место»);
- **in-document references** — «согласно 6.1.11», «таблица 6.8»;
- **embedding neighbours** — the `relation_knn_k` most similar fragments of the same document
  (cosine ≥ `relation_knn_min_cosine`), which is what finds a condition or a table far away from
  the clause it constrains. The vectors are the ones just computed for indexing.

A scorer (`relation_scorer`) then weighs both directions of each pair. `heuristic` (the default)
applies structure rules in-process: a lead-in and its items, an unnumbered continuation and its
clause, a broken phrase and a reference depend on each other. `cross_encoder` sends the pairs to the
relation-scorer service — a cross-encoder fine-tuned on LLM-labelled relations — and keeps, per
direction, the stronger of its probability and the rules; on questions labelled independently of
any scorer it added only ~5 pp of complete answers over the rules, so it is optional. `llm` asks the
configured LLM, one anchor with up to `relation_llm_group` partners per call. Directions weaker than
`relation_min_store_weight` are dropped.

Relations are stored in their own Qdrant collection (`{collection}__relations`, payload only,
deterministic id per directed pair), not in the fragment payload: a clause can depend on dozens of
others, and rescoring must not rewrite points that carry vectors. Their lifecycle follows the
fragments: deleting a document or a version removes the edges of the removed fragments; an edge whose
fragment is gone is skipped when read. A scorer failure never fails the ingest — the document is
indexed without relations.

Relations are served in search (see `api.md`: `related`, `related_to`) and by
`GET /library/documents/{doc_id}/relations`, which NormGraph mirrors as `DEPENDS_ON` edges.

## Deduplication

Before queuing the job, the upload handler extracts the text and computes `content_hash`. If such a
hash is already registered, the upload is rejected with code 400 — the text fully matches an
already-loaded document. The check is deliberately in the request rather than in the worker: a
duplicate should be a synchronous `400`, not a job that fails minutes later.

## Versioning

If the text differs, the document is loaded as a new version. The document name (`name`) identifies
the logical document under which versions are tracked. On upload:

- the `other_versions` field of the new nodes records the document's other versions already present
  in the store;
- the `other_versions` field on points of previously loaded versions is updated to include the new
  version;
- if the version string matched an existing one but the text differs, the version is made
  distinguishable by appending a short hash suffix.

## Amendments and editions

An amending act ("О внесении изменений в Правила …") is uploaded like any other document and stays
searchable on its own. It is also linked to the document it amends — through `amends` at upload
(or `PUT /documents/{name}/amends` later), or by its title: an act whose heading says "о внесении
изменений в …" is linked to the one stored document that heading names (no link when none or
several fit). A clarification is linked with `explains`; it changes no text.

Every `amends` link queues a `consolidate` job for the amended document:

1. **Root edition** — the newest edition that was uploaded rather than built. Its raw blocks are
   re-read from the original once and kept in object storage (`raw/<hash>.json`), as are an act's.
2. **Operations** — each linked act is read by the LLM (`DVD_AMENDMENT_REASONING_EFFORT`) into a
   closed list of operations: where (a path of headings — "Статья 17.1" / "Ж-2.15" — plus a
   table, a table section, table rows, a numbered item or part), what (`replace_words`,
   `append_words`, `insert`, `replace`, `delete`, `repeal`) and with what. New text is never
   retyped: the model points at the act's own blocks, and each passage is taken whole between
   « and » (balanced, so quoted names inside it survive). Words the model quotes must occur in the
   act. Appendices with maps, boundary descriptions and coordinates are not text changes. The
   item's own words then correct the model's reading, which varies from run to run: an item that
   adds («дополнить», not «изложить в … редакции») is an `insert`, one about boundary descriptions,
   coordinates or the zoning map is dropped, and missing new content is the first passage quoted
   after the item. The operations are cached on the link (with `EXTRACTOR_VERSION`), so a rebuild
   does not ask the LLM again.
3. **Application** — deterministic, act by act in date order (`effective_date`, else the date in
   the act's heading). A path resolves heading by heading; a table of contents is told apart from
   the real section by its size; nested numbering is tracked, so "part 1 / item 5" lands after
   item 4 of part 1, not after the last "4." of the article. Table rows go into the named section
   ("условно разрешенные виды"), numbered or coded rows by order (7.1 after 7, Ж-3.15.2 after
   Ж-3.15). An operation whose place, section or quoted words are not found is not guessed at: it
   is reported on its act (`failed`, with the reason) and the edition is marked for review, while
   the other operations still apply. Only a zone code ("О-8.15") may be found without the article
   the act wrongly named.
4. **Edition** — the result goes through the ordinary delta update as version
   `<root> (ред. от DD.MM.YYYY)`. Fragments built from changed blocks carry `amended_by`. The new
   edition becomes `active`; every other edition becomes `superseded`, and fragments only
   superseded editions share get `status=superseded`: default search reads the text in force,
   `version` (or `include_superseded`) reaches the old one.

An act that arrives out of order simply rebuilds from the root. An act that changes no text
(maps only) is recorded as `no_text_changes`; an act dated before the root edition as
`included`. Deleting or unlinking an act rebuilds the document without it. The acts, their
operation reports and the editions are served by `GET /documents/{name}/amendments` and shown on
the document's "Редакции" tab in the admin panel.

## Tables

Tables are stored as separate entities: nodes with `kind=table` containing `table_html`. They are
not merged with surrounding text and are available through a dedicated search endpoint.

## Neighbouring fragments and context width

The `prev_id` and `next_id` fields define the document's reading order. On search, the
`context_height` parameter specifies how many fragments before and after the match to attach: the
service walks the `prev`/`next` chain for the given number of steps and assembles the expanded text.
This allows obtaining either a pinpoint fragment or a wider context around it.

Neighbours are positional; relations are semantic. With `related=true` (the default) a search also
returns, as separate citable hits, the fragments its hits strongly depend on — the list items of a
lead-in, the condition stated in a sibling, the table a clause points at — however far away they are.

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

## Reprocessing existing documents

`dvd-parser-3` changes extracted text hashes and structure. Deploying the code does not repair
already indexed nodes. Reprocess affected source files using the document reload workflow after
reviewing the target document/editions and retaining the originals. A metadata/name backfill or
a delta update is insufficient to guarantee replacement of damaged trees.

`dvd-parser-6` keeps a document code wrapped onto the next line («СП\n17.13330», «ГОСТ\n12.4.026»)
in the clause that names the document. Such a line is not a clause number: clause numbers have at
most three digits per level, and a line ending in a designation prefix (`СП`, `ГОСТ`, `СанПиН`, `п.`,
`№`, …) is always continued. Documents ingested earlier keep the cut fragments and the shifted
clause numbers until they are reparsed (`POST /documents/reparse`), which needs the stored original.

Regression: `pytest tests/unit/test_numbering_regression.py` exercises DOCX → logical parts →
structure → hierarchy → real in-memory Qdrant search, with an adverse LLM double. It verifies that
`52 / 3.3` returns the actual provision and its editorial note, excluding the reference inside 3.2.
The separate live validation should be repeated after deployment and reprocessing.
