"""Services: IngestionService (document ingestion) and SearchService (vector search)."""

from __future__ import annotations

import difflib
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import structlog
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
)

from src.api_clients import OllamaClient, create_embedder
from src.broker.events import DocumentDeleted, DocumentProcessed, DocumentUpdated
from src.broker.outbox import EventOutbox
from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dvd_service.dto import (
    DocumentDetail,
    DocumentFragment,
    DocumentInfo,
    DocumentList,
    DocumentListResponse,
    DocumentSummary,
    DocumentUpdateResponse,
    NodePayload,
    SearchHit,
    SearchRequest,
    SearchResponse,
    TagsResponse,
)
from src.dvd_service.modules.doc_parsers import PARSER_VERSION, DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.identity import (
    build_aliases,
    build_lookup_keys,
    extract_version_from_name,
    make_span_id,
    make_version_id,
)
from src.dvd_service.modules.progress import Progress
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import VersionDetector

log = structlog.get_logger(__name__)

# Number of stages the ingest pipeline reports progress through (see ``ingest``); surfaced as
# ``stage_index``/``stage_total`` on the job status. Type-tagging now also emits fragment tags,
# so there is no separate tagging stage.
PIPELINE_STAGES = 7


def _version_condition(version: str) -> Filter:
    """Match a version against the multi-valued ``versions`` tags or the legacy ``version``."""
    return Filter(
        should=[
            FieldCondition(key="versions", match=MatchValue(value=version)),
            FieldCondition(key="version", match=MatchValue(value=version)),
        ]
    )


def build_source_url(payload: dict, name: str, version: str) -> str | None:
    """The proxied download link for a document's original file — never a raw MinIO URL.

    ``None`` when no source was stored (e.g. ingested before this feature existed). Branches on
    the payload's own ``user_id``/``scenario_id`` (not the caller's request scope), so a single
    combined-search response can correctly link both shared and user-index hits.
    """
    if not payload.get("source_object_key"):
        return None
    q_name = quote(name, safe="")
    q_version = quote(version, safe="")
    user_id = payload.get("user_id")
    scenario_id = payload.get("scenario_id")
    if user_id and scenario_id:
        return (
            f"/user-documents/{q_name}/source"
            f"?user_id={quote(user_id, safe='')}&scenario_id={quote(scenario_id, safe='')}"
            f"&version={q_version}"
        )
    return f"/documents/{q_name}/source?version={q_version}"


class IngestionService:
    def __init__(
        self,
        parser: DocumentParser,
        structure: StructureTagger,
        hierarchy: HierarchyBuilder,
        version_detector: VersionDetector,
        reference_extractor: ReferenceExtractor,
        reference_resolver: ReferenceResolver,
        qdrant: QdrantRepository,
        registry: DocumentRegistry,
        storage: DocumentStorage,
        jobs: JobStore,
        settings: Settings,
        outbox: EventOutbox | None = None,
    ) -> None:
        self.parser = parser
        self.structure = structure
        self.hierarchy = hierarchy
        self.version_detector = version_detector
        self.reference_extractor = reference_extractor
        self.reference_resolver = reference_resolver
        self.qdrant = qdrant
        self.registry = registry
        self.storage = storage
        self.jobs = jobs
        self.settings = settings
        self.outbox = outbox
        # Serializes the GPU-bound pipeline: at most ``ingest_concurrency`` documents touch the
        # LLM/embedder at once. A document waits here (status "queued") until the GPU is free, so
        # a batch upload keeps the GPU busy without oversubscribing it. Process-wide — background
        # ingest jobs run in the threadpool, hence a threading primitive.
        self._gpu_gate = threading.BoundedSemaphore(max(1, settings.ingest_concurrency))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(ingest_concurrency={self.settings.ingest_concurrency}, "
            f"embed_batch={self.settings.embed_batch}, "
            f"parser={type(self.parser).__name__}, "
            f"structure={type(self.structure).__name__}, "
            f"hierarchy={type(self.hierarchy).__name__}, "
            f"version_detector={type(self.version_detector).__name__}, "
            f"reference_extractor={type(self.reference_extractor).__name__}, "
            f"reference_resolver={type(self.reference_resolver).__name__}, "
            f"qdrant={type(self.qdrant).__name__}, "
            f"registry={type(self.registry).__name__}, "
            f"storage={type(self.storage).__name__}, "
            f"jobs={type(self.jobs).__name__}, "
            f"outbox={type(self.outbox).__name__ if self.outbox else None})"
        )

    @staticmethod
    def _numbering_index(nodes: list[dict]) -> dict[str, str]:
        """Map each distinct numbering to its node id (first occurrence wins)."""
        idx: dict[str, str] = {}
        for n in nodes:
            num = (n.get("numbering") or "").strip()
            if num and num not in idx:
                idx[num] = n["id"]
        return idx

    def _embed_all(self, texts: list[str], on_progress=None) -> list[list[float]]:
        vectors: list[list[float]] = []
        b = self.settings.embed_batch
        total = (len(texts) + b - 1) // b
        with create_embedder() as embedder:
            for done, i in enumerate(range(0, len(texts), b), 1):
                vectors.extend(embedder.embed_documents(texts[i : i + b]))
                if on_progress:
                    on_progress(done, total)
        return vectors

    def _resolve_identity(
        self,
        parts: list[dict],
        client: OllamaClient,
        name_override: str | None,
        version_override: str | None,
    ) -> tuple[str, str]:
        """Document name + version: manual overrides > 4-digit group in the name > LLM.

        The LLM detector is only invoked when either value cannot be derived without it.
        """
        name = (name_override or "").strip()
        version = (version_override or "").strip()
        if not version and name:
            version = extract_version_from_name(name) or ""
        if not name or not version:
            det_name, det_version = self.version_detector.detect(parts, client)
            name = name or det_name
            version = version or extract_version_from_name(name) or det_version
        return name, version.strip() or "unknown"

    @staticmethod
    def _match_by_text(
        base_points: list[dict], nodes: list[dict]
    ) -> tuple[dict[str, str], list[dict]]:
        """Match new nodes to stored fragments by whitespace-normalized text.

        Returns ``(id_map new-node-id -> matched point id, unmatched nodes)``. Used as the
        fallback when source-block fingerprints are unavailable (legacy versions) and for
        synthetic nodes that carry no source blocks.
        """
        pool: dict[str, list[dict]] = {}
        for p in base_points:
            key = " ".join((p.get("text") or "").split())
            pool.setdefault(key, []).append(p)
        id_map: dict[str, str] = {}
        unmatched: list[dict] = []
        for n in nodes:
            bucket = pool.get(" ".join(n["text"].split()))
            if bucket:
                id_map[n["id"]] = bucket.pop(0)["id"]
            else:
                unmatched.append(n)
        return id_map, unmatched

    @staticmethod
    def _match_by_blocks(
        base_points: list[dict],
        nodes: list[dict],
        old_hashes: list[str],
        new_hashes: list[str],
    ) -> tuple[set[str], set[str], dict[str, str]]:
        """Deterministic source-level matching: diff the raw-block hashes of two editions.

        A stored fragment is reused iff every source block it covers is unchanged; new nodes
        are inserted iff they touch a changed/added block or fall outside the reused coverage.
        Immune to LLM fragmentation drift, since the raw blocks are parsed deterministically.

        Returns ``(reused point ids, node ids to insert, id_map new-node-id -> covering point)``.
        """
        matcher = difflib.SequenceMatcher(a=old_hashes, b=new_hashes, autojunk=False)
        old_to_new: dict[int, int] = {}
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    old_to_new[i1 + k] = j1 + k

        # Candidate reuse: stored fragments whose every source block is unchanged,
        # keyed by the new-edition block indices they cover.
        reuse: dict[str, set[int]] = {}
        for p in base_points:
            blocks = p.get("src_block_ids") or []
            if blocks and all(b in old_to_new for b in blocks):
                reuse[p["id"]] = {old_to_new[b] for b in blocks}

        node_blocks = {n["id"]: set(n.get("src_ids") or []) for n in nodes}
        # Fixpoint: inserted nodes may straddle an edit boundary and overlap a reusable
        # fragment's blocks — drop such fragments from reuse so no block is stored twice.
        while True:
            covered: set[int] = set().union(*reuse.values()) if reuse else set()
            insert_ids = {
                nid
                for nid, blocks in node_blocks.items()
                if blocks and not blocks <= covered
            }
            inserted_blocks: set[int] = (
                set().union(*(node_blocks[i] for i in insert_ids))
                if insert_ids
                else set()
            )
            conflicted = [
                pid for pid, blocks in reuse.items() if blocks & inserted_blocks
            ]
            if not conflicted:
                break
            for pid in conflicted:
                del reuse[pid]

        # Link remapping: a skipped node points at the reused fragment covering its blocks.
        id_map: dict[str, str] = {}
        for nid, blocks in node_blocks.items():
            if nid in insert_ids or not blocks:
                continue
            for pid, covered_blocks in reuse.items():
                if blocks <= covered_blocks:
                    id_map[nid] = pid
                    break
        return set(reuse), insert_ids, id_map

    @staticmethod
    def _version_tags(payload: dict) -> list[str]:
        """Every version a stored fragment belongs to (legacy points carry only ``version``)."""
        tags = payload.get("versions") or []
        if not tags and payload.get("version"):
            tags = [payload["version"]]
        return list(tags)

    def _resolve_version(
        self, name: str, version: str, content_hash: str
    ) -> tuple[str, list[str]]:
        """Version + the list of OTHER versions of this document in the database.

        Exact duplicates are filtered out earlier (by hash). If the version string matches but the
        text differs, make the version distinguishable with a hash suffix.
        """
        existing = self.registry.versions(name)
        if version in existing:
            version = f"{version} (ред. {content_hash[:6]})"
        other_versions = sorted(v for v in existing if v != version)
        return version, other_versions

    @staticmethod
    def _grounding(node: dict, spans: list[dict], doc_id: str) -> dict:
        """Source offsets/page/bbox/span_id for a node, derived from its source elements."""
        ids = [
            i
            for i in node.get("src_ids", [])
            if isinstance(i, int) and 0 <= i < len(spans)
        ]
        if not ids:
            return {
                "char_start": None,
                "char_end": None,
                "page_start": None,
                "page_end": None,
                "bbox": None,
                "span_id": None,
            }
        char_start = min(spans[i]["start"] for i in ids)
        char_end = max(spans[i]["end"] for i in ids)
        pages = [spans[i]["page"] for i in ids if spans[i]["page"] is not None]
        bbox = next((spans[i]["bbox"] for i in ids if spans[i]["bbox"]), None)
        return {
            "char_start": char_start,
            "char_end": char_end,
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
            "bbox": bbox,
            "span_id": make_span_id(doc_id, char_start, char_end),
        }

    def _build_points(
        self,
        nodes: list[dict],
        vectors: list[list[float]],
        spans: list[dict],
        doc_id: str,
        node_tags: dict[str, list],
        node_refs: dict[str, list],
        identity: dict,
    ) -> list[PointStruct]:
        """Assemble Qdrant points for nodes; ``identity`` holds the shared payload fields."""
        return [
            PointStruct(
                id=n["id"],
                vector=vec,
                payload=NodePayload(
                    doc_id=doc_id,
                    kind=n["kind"],
                    type=n["type"],
                    numbering=n["numbering"],
                    block=n["block"],
                    depth=n["depth"],
                    order=n["_order"],
                    parent_id=n["parent_id"],
                    parent_text=n["parent_text"],
                    child_ids=n["child_ids"],
                    prev_id=n["prev_id"],
                    next_id=n["next_id"],
                    breadcrumb=n["breadcrumb"],
                    tags=node_tags.get(n["id"], []),
                    table_html=n["table_html"],
                    references=node_refs.get(n["id"], []),
                    src_block_ids=n.get("src_ids", []),
                    text=n["text"],
                    **identity,
                    **self._grounding(n, spans, doc_id),
                ).model_dump(),
            )
            for n, vec in zip(nodes, vectors)
        ]

    def ingest(
        self,
        file_path: str,
        raw: list[dict],
        content_hash: str,
        version_override: str | None = None,
        doc_id: str | None = None,
        job_id: str | None = None,
        *,
        name_override: str | None = None,
        emit_event: bool = True,
        doc_type: str | None = None,
        corpus: str | None = None,
        lang: str | None = None,
        title: str | None = None,
        source_uri: str | None = None,
        source_object_key: str | None = None,
        external_ids: dict | None = None,
        metadata: dict | None = None,
        effective_date: str | None = None,
    ) -> dict:
        doc_id = doc_id or str(uuid.uuid4())
        # Block until a GPU slot is free: the job stays "queued" while it waits, flips to
        # "processing" only once it holds the slot (see ``_gpu_gate``). Released in ``finally``.
        if job_id:
            self.jobs.update(job_id, status="queued")
        self._gpu_gate.acquire()
        client = OllamaClient()
        try:
            if job_id:
                self.jobs.update(job_id, status="processing")
            progress = Progress(self.jobs, job_id, total_stages=PIPELINE_STAGES)
            progress.stage("structure-markup")
            parts = self.parser.to_logical_parts(
                raw, client, on_progress=progress.advance
            )  # Stage 1 + 1.5

            progress.stage("type-tagging")
            self.structure.tag(
                parts, client, on_progress=progress.advance
            )  # Stage 2 + 3 (structural fields + fragment tags in one pass)

            progress.stage("hierarchy")
            ranks = self.structure.numbering_ranks(parts)  # Stage 3.5
            tree = self.hierarchy.build(parts, ranks, title=Path(file_path).stem)
            self.hierarchy.cap_unnumbered_nesting(tree)
            self.hierarchy.group_amendment(tree)
            nodes = self.hierarchy.flatten(tree)  # prev/next, kind, html

            progress.stage("identity")
            name, version = self._resolve_identity(
                parts, client, name_override, version_override
            )
            version, other_versions = self._resolve_version(name, version, content_hash)

            # Fragment tags were produced together with the structural fields (see
            # ``StructureTagger.tag``) and ride the hierarchy onto each node — no extra LLM pass.
            node_tags = {n["id"]: n.get("tags", []) for n in nodes}
            uploaded_at = datetime.now(timezone.utc).isoformat()

            # Stage: extract + resolve references (links to other documents/clauses)
            progress.stage("references")
            numbering_index = self._numbering_index(nodes)
            node_refs: dict[str, list] = {}
            if self.settings.enable_reference_linking:
                raw_refs = self.reference_extractor.extract(
                    nodes, client, on_progress=progress.advance
                )
                node_refs = self.reference_resolver.resolve(
                    doc_id, name, raw_refs, numbering_index
                )

            progress.stage("embeddings")
            vectors = self._embed_all(
                [n["text"] for n in nodes], on_progress=progress.advance
            )

            # --- general-purpose identity + provenance (shared by all consumers) ---
            _, spans = self.parser.source_index(raw)
            external_ids = external_ids or {}
            doc_type = doc_type or self.settings.default_doc_type
            corpus = corpus or self.settings.default_corpus
            lang = lang or self.settings.default_lang
            version_id = make_version_id(name, content_hash)
            aliases = build_aliases(name, external_ids)
            lookup_keys = build_lookup_keys(name, external_ids)
            embedding_meta = {
                "model": self.settings.embedding_model_name,
                "dim": self.settings.vector_size,
                "metric": "cosine",
                "normalized": True,
            }

            for order, n in enumerate(nodes):
                n["_order"] = order
            identity = {
                "parser_version": PARSER_VERSION,
                "embedding_meta": embedding_meta,
                "name": name,
                "title": title,
                "version": version,
                "versions": [version],
                "version_id": version_id,
                "other_versions": other_versions,
                "content_hash": content_hash,
                "doc_type": doc_type,
                "corpus": corpus,
                "lang": lang,
                "external_ids": external_ids,
                "aliases": aliases,
                "lookup_keys": lookup_keys,
                "effective_date": effective_date,
                "source": os.path.basename(file_path),
                "source_uri": source_uri or os.path.basename(file_path),
                "source_object_key": source_object_key,
                "metadata": metadata or {},
                "uploaded_at": uploaded_at,
            }
            progress.stage("indexing")
            points = self._build_points(
                nodes, vectors, spans, doc_id, node_tags, node_refs, identity
            )
            count = self.qdrant.upsert(points)

            # For already-loaded versions, refresh their list of other versions (including the new one)
            all_versions = set(other_versions) | {version}
            for v in other_versions:
                self.qdrant.set_other_versions(name, v, sorted(all_versions - {v}))
            self.registry.register(content_hash, name, version, doc_id)
            self.registry.register_blocks(
                name, version, DocumentParser.block_hashes(raw)
            )
            self.registry.register_document(
                doc_id,
                {
                    "doc_id": doc_id,
                    "name": name,
                    "title": title,
                    "version": version,
                    "version_id": version_id,
                    "other_versions": other_versions,
                    "doc_type": doc_type,
                    "corpus": corpus,
                    "lang": lang,
                    "status": "active",
                    "external_ids": external_ids,
                    "source_uri": source_uri or os.path.basename(file_path),
                    "source_object_key": source_object_key,
                    "content_hash": content_hash,
                    "node_count": count,
                    "uploaded_at": uploaded_at,
                    "effective_date": effective_date,
                    "metadata": metadata or {},
                    "tags": sorted(
                        {tag for values in node_tags.values() for tag in values}
                    ),
                },
            )

            # Complete dangling references from earlier documents that pointed at this one
            if self.settings.enable_reference_linking:
                self.reference_resolver.backfill(name, doc_id, version, numbering_index)

            # Announce the fully processed document to downstream services (Kafka,
            # via the durable outbox — the publisher delivers it asynchronously).
            # ``emit_event`` is off when a caller (reload) announces the outcome itself.
            if self.outbox is not None and emit_event:
                self.outbox.enqueue(DocumentProcessed(document_name=name))

            result = {
                "doc_id": doc_id,
                "name": name,
                "version": version,
                "other_versions": other_versions,
                "nodes": count,
            }
            if job_id:
                self.jobs.update(job_id, status="done", **result)
            log.info("ingest_done", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest_failed", doc_id=doc_id)
            if job_id:
                self.jobs.update(job_id, status="error", error=str(exc))
            raise
        finally:
            client.close()
            self._gpu_gate.release()

    def update(
        self,
        name: str,
        file_path: str,
        raw: list[dict],
        content_hash: str,
        version_override: str | None = None,
        job_id: str | None = None,
        *,
        doc_type: str | None = None,
        corpus: str | None = None,
        lang: str | None = None,
        title: str | None = None,
        source_uri: str | None = None,
        source_object_key: str | None = None,
        external_ids: dict | None = None,
        metadata: dict | None = None,
        effective_date: str | None = None,
    ) -> dict:
        """Delta update of an existing document under a new version.

        Fragments whose text is unchanged against the latest stored version just receive the
        new version tag (``versions``); changed/added fragments go through the full pipeline
        (structure, tagging, embedding) and are inserted next to them, tagged with the new
        version only. Old versions stay searchable untouched.
        """
        if job_id:
            self.jobs.update(job_id, status="queued")
        self._gpu_gate.acquire()
        client = OllamaClient()
        try:
            if job_id:
                self.jobs.update(job_id, status="processing")
            existing = self.qdrant.points_by_name(name)
            if not existing:
                raise KeyError(f"документ не найден: {name}")
            # Compare against the latest stored version — the edition being amended.
            base_version = max(
                self.registry.versions(name)
                or sorted({t for p in existing for t in self._version_tags(p)}),
            )
            doc_id = next(
                (
                    p["doc_id"]
                    for p in existing
                    if base_version in self._version_tags(p) and p.get("doc_id")
                ),
                existing[0].get("doc_id") or str(uuid.uuid4()),
            )

            parts = self.parser.to_logical_parts(raw, client)  # Stage 1 + 1.5
            self.structure.tag(parts, client)  # Stage 2 + 3
            ranks = self.structure.numbering_ranks(parts)  # Stage 3.5
            tree = self.hierarchy.build(parts, ranks, title=Path(file_path).stem)
            self.hierarchy.cap_unnumbered_nesting(tree)
            self.hierarchy.group_amendment(tree)
            nodes = self.hierarchy.flatten(tree)

            _, version = self._resolve_identity(parts, client, name, version_override)
            version, other_versions = self._resolve_version(name, version, content_hash)

            for order, n in enumerate(nodes):
                n["_order"] = order
            base_points = [p for p in existing if base_version in self._version_tags(p)]

            # Delta detection. Preferred path: deterministic diff of the source-block
            # fingerprints — reuse is decided by what actually changed in the source,
            # immune to LLM fragmentation drift. Nodes without source blocks (synthetic
            # containers) and legacy versions without fingerprints match by text.
            old_hashes = self.registry.get_blocks(name, base_version)
            new_hashes = DocumentParser.block_hashes(raw)
            if old_hashes:
                reused_ids, insert_ids, id_map = self._match_by_blocks(
                    base_points, nodes, old_hashes, new_hashes
                )
                text_map, text_new = self._match_by_text(
                    [
                        p
                        for p in base_points
                        if not p.get("src_block_ids") and p["id"] not in reused_ids
                    ],
                    [n for n in nodes if not n.get("src_ids")],
                )
                id_map.update(text_map)
                reused_ids |= set(text_map.values())
                new_nodes = sorted(
                    [n for n in nodes if n["id"] in insert_ids] + text_new,
                    key=lambda n: n["_order"],
                )
            else:
                id_map, new_nodes = self._match_by_text(base_points, nodes)
                reused_ids = set(id_map.values())

            # Unchanged fragments: just tag them with the new version.
            tag_groups: dict[tuple, list[str]] = {}
            for p in existing:
                if p["id"] not in reused_ids:
                    continue
                tags = self._version_tags(p)
                if version not in tags:
                    tag_groups.setdefault(tuple(tags + [version]), []).append(p["id"])
            for tags, ids in tag_groups.items():
                self.qdrant.set_versions(ids, list(tags))

            # Changed/added fragments: full pipeline, links remapped onto shared points.
            def _mapped(node_id: str | None) -> str | None:
                return id_map.get(node_id, node_id) if node_id else node_id

            for n in new_nodes:
                n["parent_id"] = _mapped(n["parent_id"])
                n["prev_id"] = _mapped(n["prev_id"])
                n["next_id"] = _mapped(n["next_id"])
                n["child_ids"] = [_mapped(c) for c in n["child_ids"]]

            node_tags = {n["id"]: n.get("tags", []) for n in new_nodes}
            node_refs: dict[str, list] = {}
            if self.settings.enable_reference_linking and new_nodes:
                numbering_index = {
                    num: _mapped(nid)
                    for num, nid in self._numbering_index(nodes).items()
                }
                raw_refs = self.reference_extractor.extract(new_nodes, client)
                node_refs = self.reference_resolver.resolve(
                    doc_id, name, raw_refs, numbering_index
                )

            vectors = (
                self._embed_all([n["text"] for n in new_nodes]) if new_nodes else []
            )
            _, spans = self.parser.source_index(raw)
            external_ids = external_ids or {}
            uploaded_at = datetime.now(timezone.utc).isoformat()
            identity = {
                "parser_version": PARSER_VERSION,
                "embedding_meta": {
                    "model": self.settings.embedding_model_name,
                    "dim": self.settings.vector_size,
                    "metric": "cosine",
                    "normalized": True,
                },
                "name": name,
                "title": title,
                "version": version,
                "versions": [version],
                "version_id": make_version_id(name, content_hash),
                "other_versions": other_versions,
                "content_hash": content_hash,
                "doc_type": doc_type or self.settings.default_doc_type,
                "corpus": corpus or self.settings.default_corpus,
                "lang": lang or self.settings.default_lang,
                "external_ids": external_ids,
                "aliases": build_aliases(name, external_ids),
                "lookup_keys": build_lookup_keys(name, external_ids),
                "effective_date": effective_date,
                "source": os.path.basename(file_path),
                "source_uri": source_uri or os.path.basename(file_path),
                "source_object_key": source_object_key,
                "metadata": metadata or {},
                "uploaded_at": uploaded_at,
            }
            points = self._build_points(
                new_nodes, vectors, spans, doc_id, node_tags, node_refs, identity
            )
            count = self.qdrant.upsert(points)

            all_versions = set(other_versions) | {version}
            for v in other_versions:
                self.qdrant.set_other_versions(name, v, sorted(all_versions - {v}))
            self.registry.register(content_hash, name, version, doc_id)
            self.registry.register_blocks(name, version, new_hashes)
            self.registry.register_document(
                doc_id,
                {
                    "doc_id": doc_id,
                    "name": name,
                    "title": title,
                    "version": version,
                    "version_id": identity["version_id"],
                    "other_versions": other_versions,
                    "doc_type": identity["doc_type"],
                    "corpus": identity["corpus"],
                    "lang": identity["lang"],
                    "status": "active",
                    "external_ids": external_ids,
                    "source_uri": identity["source_uri"],
                    "source_object_key": source_object_key,
                    "content_hash": content_hash,
                    "node_count": len(reused_ids) + count,
                    "uploaded_at": uploaded_at,
                    "effective_date": effective_date,
                    "metadata": metadata or {},
                    "tags": sorted(
                        {tag for point in base_points for tag in point.get("tags", [])}
                        | {tag for values in node_tags.values() for tag in values}
                    ),
                },
            )

            if self.outbox is not None:
                self.outbox.enqueue(
                    DocumentUpdated(document_name=name, version=version)
                )

            result = {
                "doc_id": doc_id,
                "name": name,
                "version": version,
                "other_versions": other_versions,
                "nodes": len(reused_ids) + count,
                "new_nodes": count,
                "reused_nodes": len(reused_ids),
            }
            if job_id:
                self.jobs.update(job_id, status="done", **result)
            log.info("update_done", **result)
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("update_failed", name=name)
            if job_id:
                self.jobs.update(job_id, status="error", error=str(exc))
            raise
        finally:
            client.close()
            self._gpu_gate.release()

    def reload(
        self,
        name: str,
        file_path: str,
        raw: list[dict],
        content_hash: str,
        version_override: str | None = None,
        job_id: str | None = None,
        **meta,
    ) -> dict:
        """Full reload: wipe every stored version of the document, then ingest from scratch.

        Create-or-replace semantics — a document that is not stored yet is simply ingested.
        Announces a single event describing the outcome: ``DocumentUpdated`` when an existing
        document was replaced (no intermediate ``DocumentDeleted``), ``DocumentProcessed``
        when the reload effectively created the document.
        """
        if job_id:
            self.jobs.update(job_id, status="processing")
        replaced = True
        try:
            self.delete_document(name, emit_event=False)
        except KeyError:
            replaced = False  # nothing stored under this name yet
        except Exception as exc:  # noqa: BLE001
            if job_id:
                self.jobs.update(job_id, status="error", error=str(exc))
            raise
        result = self.ingest(
            file_path,
            raw,
            content_hash,
            version_override=version_override,
            job_id=job_id,
            name_override=name,
            emit_event=False,
            **meta,
        )
        if self.outbox is not None:
            if replaced:
                self.outbox.enqueue(
                    DocumentUpdated(document_name=name, version=result["version"])
                )
            else:
                self.outbox.enqueue(DocumentProcessed(document_name=name))
        return result

    def delete_document(
        self, name: str, version: str | None = None, *, emit_event: bool = True
    ) -> dict:
        """Remove a document (or one of its versions) from Qdrant and the Redis registry.

        Deleting a single version drops the fragments that belong to it exclusively and only
        removes the version tag from fragments shared with other versions. Announces a
        ``DocumentDeleted`` event unless the caller (reload) reports the outcome itself.
        """
        existing = self.qdrant.points_by_name(name)
        known_versions = self.registry.versions(name)
        if not existing and not known_versions:
            raise KeyError(f"документ не найден: {name}")

        if version is None:
            versions_removed = sorted(
                set(known_versions)
                | {t for p in existing for t in self._version_tags(p)}
            )
            doc_ids = {p["doc_id"] for p in existing if p.get("doc_id")}
            source_keys = {p["source_object_key"] for p in existing if p.get("source_object_key")}
            self.qdrant.delete_by_name(name)
            self.registry.remove_hashes(name)
            for did in doc_ids:
                self.registry.unregister_document(did)
            self.registry.unregister_name(name)
            for key in source_keys:
                self.storage.delete(key)
            result = {
                "name": name,
                "versions_removed": versions_removed,
                "points_deleted": len(existing),
                "points_updated": 0,
            }
            if self.outbox is not None and emit_event:
                self.outbox.enqueue(
                    DocumentDeleted(
                        document_name=name,
                        versions_removed=versions_removed,
                        document_removed=True,
                    )
                )
            log.info("document_deleted", **result)
            return result

        tagged = [p for p in existing if version in self._version_tags(p)]
        if not tagged and version not in known_versions:
            raise KeyError(f"версия не найдена: {name} / {version}")
        # The fragments this version's ingest/update call actually stamped its source_object_key
        # onto — ``version`` records "the version a fragment first appeared in", so this is the
        # correct (and only) place that key lives, regardless of which fragments are later shared.
        origin_key = next(
            (
                p["source_object_key"]
                for p in existing
                if p.get("version") == version and p.get("source_object_key")
            ),
            None,
        )
        to_delete = [p["id"] for p in tagged if set(self._version_tags(p)) == {version}]
        tag_groups: dict[tuple, list[str]] = {}
        for p in tagged:
            remaining_tags = [t for t in self._version_tags(p) if t != version]
            if remaining_tags:
                tag_groups.setdefault(tuple(remaining_tags), []).append(p["id"])
        self.qdrant.delete_points(to_delete)
        for tags, ids in tag_groups.items():
            self.qdrant.set_versions(ids, list(tags))

        self.registry.remove_version(name, version)
        self.registry.remove_hashes(name, version)
        for rec in self.registry.all_documents():
            if rec.get("name") == name and rec.get("version") == version:
                self.registry.unregister_document(rec["doc_id"])

        remaining_versions = self.registry.versions(name)
        for v in remaining_versions:
            self.qdrant.set_other_versions(
                name, v, sorted(set(remaining_versions) - {v})
            )
        document_removed = not remaining_versions and len(to_delete) == len(existing)
        if document_removed:
            self.registry.unregister_name(name)
        if origin_key:
            self.storage.delete(origin_key)

        result = {
            "name": name,
            "versions_removed": [version],
            "points_deleted": len(to_delete),
            "points_updated": sum(len(ids) for ids in tag_groups.values()),
        }
        if self.outbox is not None and emit_event:
            self.outbox.enqueue(
                DocumentDeleted(
                    document_name=name,
                    versions_removed=[version],
                    document_removed=document_removed,
                )
            )
        log.info("document_version_deleted", **result)
        return result


class SearchService:
    def __init__(self, qdrant: QdrantRepository, settings: Settings) -> None:
        self.qdrant = qdrant
        self.settings = settings

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(qdrant={type(self.qdrant).__name__}, "
            f"search_limit={self.settings.search_limit}, "
            f"max_context_height={self.settings.max_context_height})"
        )

    def _build_filter(self, req: SearchRequest, kind: str | None) -> Filter | None:
        must = []
        if kind:
            must.append(FieldCondition(key="kind", match=MatchValue(value=kind)))
        if req.name:
            must.append(FieldCondition(key="name", match=MatchValue(value=req.name)))
        if req.version:
            must.append(_version_condition(req.version))
        if req.block:
            must.append(FieldCondition(key="block", match=MatchValue(value=req.block)))
        if req.types:
            must.append(FieldCondition(key="type", match=MatchAny(any=req.types)))
        if req.doc_id:
            must.append(
                FieldCondition(key="doc_id", match=MatchValue(value=req.doc_id))
            )
        if req.doc_type:
            must.append(
                FieldCondition(key="doc_type", match=MatchValue(value=req.doc_type))
            )
        if req.corpus:
            must.append(
                FieldCondition(key="corpus", match=MatchValue(value=req.corpus))
            )
        if req.lang:
            must.append(FieldCondition(key="lang", match=MatchValue(value=req.lang)))
        if req.tags:
            must.append(FieldCondition(key="tags", match=MatchAny(any=req.tags)))
        if req.document_names:
            must.append(
                FieldCondition(key="name", match=MatchAny(any=req.document_names))
            )
        return Filter(must=must) if must else None

    def _expand_context(self, payload: dict, height: int) -> str:
        """Append `height` fragments before and after along the prev_id/next_id chain."""
        height = max(0, min(height, self.settings.max_context_height))
        texts = [payload.get("text", "")]
        cur = payload
        for _ in range(height):
            pid = cur.get("prev_id")
            if not pid:
                break
            got = self.qdrant.retrieve([pid])
            cur = got.get(pid)
            if not cur:
                break
            texts.insert(0, cur.get("text", ""))
        cur = payload
        for _ in range(height):
            nid = cur.get("next_id")
            if not nid:
                break
            got = self.qdrant.retrieve([nid])
            cur = got.get(nid)
            if not cur:
                break
            texts.append(cur.get("text", ""))
        return " ".join(t for t in texts if t)

    def search(self, req: SearchRequest, kind: str | None = None) -> SearchResponse:
        with create_embedder() as embedder:
            vector = embedder.embed_query(req.query)
        limit = req.limit or self.settings.search_limit
        points = self.qdrant.search(vector, self._build_filter(req, kind), limit)

        hits = []
        for p in points:
            pl = p.payload or {}
            context = (
                self._expand_context(pl, req.context_height)
                if req.context_height
                else None
            )
            hits.append(
                SearchHit(
                    id=str(p.id),
                    score=p.score,
                    doc_id=pl.get("doc_id", ""),
                    name=pl.get("name", ""),
                    title=pl.get("title"),
                    version=pl.get("version", ""),
                    versions=pl.get("versions", []) or [],
                    version_id=pl.get("version_id"),
                    other_versions=pl.get("other_versions", []) or [],
                    doc_type=pl.get("doc_type", "document"),
                    corpus=pl.get("corpus", "default"),
                    lang=pl.get("lang"),
                    external_ids=pl.get("external_ids", {}) or {},
                    kind=pl.get("kind", "text"),
                    type=pl.get("type", ""),
                    block=pl.get("block", "main"),
                    numbering=pl.get("numbering", ""),
                    breadcrumb=pl.get("breadcrumb", ""),
                    depth=pl.get("depth", 0) or 0,
                    order=pl.get("order", 0) or 0,
                    parent_id=pl.get("parent_id"),
                    prev_id=pl.get("prev_id"),
                    next_id=pl.get("next_id"),
                    source_uri=pl.get("source_uri"),
                    source_file_url=build_source_url(
                        pl, pl.get("name", ""), pl.get("version", "")
                    ),
                    char_start=pl.get("char_start"),
                    char_end=pl.get("char_end"),
                    page_start=pl.get("page_start"),
                    page_end=pl.get("page_end"),
                    span_id=pl.get("span_id"),
                    tags=pl.get("tags", []) or [],
                    metadata=pl.get("metadata", {}) or {},
                    references=pl.get("references", []) or [],
                    text=pl.get("text", ""),
                    context=context,
                    table_html=pl.get("table_html"),
                )
            )
        return SearchResponse(count=len(hits), hits=hits)


class DocumentsService:
    """Lists documents already in the store, aggregated from their fragment payloads.

    The registry (Redis) only tracks name/version existence; per-document facts shown here
    (node count, blocks present, tag union, upload time) live on the fragments themselves, so
    they are computed by scrolling Qdrant and grouping by ``(name, version)``.
    """

    def __init__(self, qdrant: QdrantRepository) -> None:
        self.qdrant = qdrant

    def __repr__(self) -> str:
        return f"{type(self).__name__}(qdrant={type(self.qdrant).__name__})"

    @staticmethod
    def _build_filter(
        name: str | None, version: str | None, block: str | None, tags: list[str] | None
    ) -> Filter | None:
        must = []
        if name:
            must.append(FieldCondition(key="name", match=MatchValue(value=name)))
        if version:
            must.append(_version_condition(version))
        if block:
            must.append(FieldCondition(key="block", match=MatchValue(value=block)))
        if tags:
            must.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
        return Filter(must=must) if must else None

    def list_documents(
        self,
        name: str | None = None,
        version: str | None = None,
        block: str | None = None,
        tags: list[str] | None = None,
        uploaded_from: str | None = None,
        uploaded_to: str | None = None,
    ) -> DocumentListResponse:
        """Aggregated, per-document view, optionally narrowed by the given filters.

        ``uploaded_from``/``uploaded_to`` compare against the ISO 8601 ``uploaded_at`` timestamp
        as plain strings (lexicographic order matches chronological order for ISO 8601) and are
        applied after aggregation, since upload time is a per-document fact, not an indexed
        per-fragment field.
        """
        payloads = self.qdrant.scroll_payloads(
            self._build_filter(name, version, block, tags)
        )

        groups: dict[tuple[str, str], dict] = {}
        for pl, doc_version in (
            (pl, v)
            for pl in payloads
            for v in (pl.get("versions") or [pl.get("version", "")])
        ):
            # A shared fragment belongs to several versions; count it under each. When a
            # version filter is set, other tags of matched fragments must not leak through.
            if version and doc_version != version:
                continue
            key = (pl.get("name", ""), doc_version)
            g = groups.get(key)
            if g is None:
                g = {
                    "doc_id": pl.get("doc_id", ""),
                    "other_versions": pl.get("other_versions", []) or [],
                    "source": pl.get("source"),
                    "uploaded_at": pl.get("uploaded_at") or None,
                    "scenario_id": pl.get("scenario_id"),
                    "user_id": pl.get("user_id"),
                    "source_object_key": None,
                    "blocks": set(),
                    "tags": set(),
                    "node_count": 0,
                }
                groups[key] = g
            g["blocks"].add(pl.get("block", "main"))
            g["tags"].update(pl.get("tags", []) or [])
            g["node_count"] += 1
            if not g["uploaded_at"] and pl.get("uploaded_at"):
                g["uploaded_at"] = pl["uploaded_at"]
            # Prefer the fragment that actually originated this version (its ``version`` field,
            # not just ``versions`` membership) — a fragment shared from an older version still
            # carries that older version's key, which would otherwise link the wrong file.
            if pl.get("source_object_key") and (
                pl.get("version") == doc_version or not g["source_object_key"]
            ):
                g["source_object_key"] = pl["source_object_key"]

        documents = []
        for (doc_name, doc_version), g in groups.items():
            uploaded_at = g["uploaded_at"]
            if uploaded_from and (not uploaded_at or uploaded_at < uploaded_from):
                continue
            if uploaded_to and (not uploaded_at or uploaded_at > uploaded_to):
                continue
            documents.append(
                DocumentInfo(
                    doc_id=g["doc_id"],
                    name=doc_name,
                    version=doc_version,
                    other_versions=g["other_versions"],
                    blocks=sorted(g["blocks"]),
                    tags=sorted(g["tags"]),
                    node_count=g["node_count"],
                    uploaded_at=uploaded_at,
                    source=g["source"],
                    scenario_id=g["scenario_id"],
                    source_file_url=build_source_url(
                        {
                            "source_object_key": g["source_object_key"],
                            "user_id": g["user_id"],
                            "scenario_id": g["scenario_id"],
                        },
                        doc_name,
                        doc_version,
                    ),
                )
            )
        documents.sort(key=lambda d: (d.name, d.version))
        return DocumentListResponse(count=len(documents), documents=documents)


class LibraryService:
    """Document-level read API: enumerate documents and fetch one by ``doc_id``.

    The MSI-TSIM-facing surface — returns a document as assembled text + metadata + ordered
    fragments (each with source grounding), complementing semantic search.
    """

    def __init__(self, qdrant: QdrantRepository, registry: DocumentRegistry) -> None:
        self.qdrant = qdrant
        self.registry = registry

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(qdrant={type(self.qdrant).__name__}, "
            f"registry={type(self.registry).__name__})"
        )

    @staticmethod
    def _summary_from_record(rec: dict) -> DocumentSummary:
        return DocumentSummary(
            **{k: rec[k] for k in DocumentSummary.model_fields if k in rec},
            source_file_url=build_source_url(
                rec, rec.get("name", ""), rec.get("version", "")
            ),
        )

    @staticmethod
    def _summary_from_payload(pl: dict, node_count: int) -> DocumentSummary:
        """Fallback summary for documents ingested before the registry stored a record."""
        return DocumentSummary(
            doc_id=pl.get("doc_id", ""),
            name=pl.get("name", ""),
            title=pl.get("title"),
            version=pl.get("version", ""),
            version_id=pl.get("version_id"),
            other_versions=pl.get("other_versions", []) or [],
            doc_type=pl.get("doc_type", "document"),
            corpus=pl.get("corpus", "default"),
            lang=pl.get("lang"),
            status=pl.get("status", "active"),
            external_ids=pl.get("external_ids", {}) or {},
            source_uri=pl.get("source_uri"),
            source_file_url=build_source_url(
                pl, pl.get("name", ""), pl.get("version", "")
            ),
            content_hash=pl.get("content_hash"),
            node_count=node_count,
            uploaded_at=pl.get("uploaded_at"),
            effective_date=pl.get("effective_date"),
            metadata=pl.get("metadata", {}) or {},
            tags=pl.get("tags", []) or [],
        )

    def list_documents(self) -> DocumentList:
        docs = [self._summary_from_record(r) for r in self.registry.all_documents()]
        return DocumentList(count=len(docs), documents=docs)

    def get_document(self, doc_id: str) -> DocumentDetail | None:
        payloads = self.qdrant.list_by_doc(doc_id)
        if not payloads:
            return None
        payloads.sort(key=lambda pl: pl.get("order", 0) or 0)

        rec = self.registry.get_document(doc_id)
        summary = (
            self._summary_from_record({**rec, "node_count": len(payloads)})
            if rec
            else self._summary_from_payload(payloads[0], len(payloads))
        )
        fragments = [
            DocumentFragment(
                **{k: pl.get(k) for k in DocumentFragment.model_fields if k in pl}
            )
            for pl in payloads
        ]
        text = "\n".join(f.text for f in fragments if f.text)
        return DocumentDetail(**summary.model_dump(), text=text, fragments=fragments)

    def find_documents(self, key: str) -> DocumentList:
        """Resolve documents by an exact lookup key / external id value."""
        doc_ids = self.qdrant.doc_ids_by_lookup_key(key)
        docs: list[DocumentSummary] = []
        for did in doc_ids:
            rec = self.registry.get_document(did)
            if rec:
                docs.append(self._summary_from_record(rec))
        return DocumentList(count=len(docs), documents=docs)


class DocumentEditorService:
    """Manual edits that keep Qdrant payloads, Redis summaries and embeddings coherent."""

    DOCUMENT_FIELDS = {
        "title",
        "doc_type",
        "corpus",
        "lang",
        "status",
        "effective_date",
        "external_ids",
        "metadata",
        "tags",
    }
    FRAGMENT_FIELDS = {"text", "tags", "metadata", "table_html"}

    def __init__(
        self,
        qdrant: QdrantRepository,
        registry: DocumentRegistry,
        settings: Settings,
    ) -> None:
        self.qdrant = qdrant
        self.registry = registry
        self.settings = settings

    def update_document(self, doc_id: str, updates: dict) -> DocumentUpdateResponse:
        points = self.qdrant.list_by_doc(doc_id)
        if not points:
            raise KeyError("document not found")
        changes = {k: v for k, v in updates.items() if k in self.DOCUMENT_FIELDS}
        if not changes:
            raise ValueError("no editable fields supplied")

        first = points[0]
        if "external_ids" in changes:
            external_ids = changes["external_ids"] or {}
            changes["aliases"] = build_aliases(first.get("name", ""), external_ids)
            changes["lookup_keys"] = build_lookup_keys(
                first.get("name", ""), external_ids
            )
        changes["manual_edited_at"] = datetime.now(timezone.utc).isoformat()
        self.qdrant.set_document_payload(doc_id, changes)

        record = self.registry.get_document(doc_id) or {
            k: first.get(k) for k in DocumentSummary.model_fields
        }
        record.update(
            {k: v for k, v in changes.items() if k in DocumentSummary.model_fields}
        )
        self.registry.register_document(doc_id, record)
        return DocumentUpdateResponse(
            doc_id=doc_id,
            points_updated=len(points),
            fields_updated=sorted(k for k in changes if k != "manual_edited_at"),
        )

    def update_fragment(self, doc_id: str, fragment_id: str, updates: dict) -> dict:
        point = self.qdrant.get_point(fragment_id)
        if point is None:
            raise KeyError("fragment not found")
        vector, payload = point
        if payload.get("doc_id") != doc_id:
            raise KeyError("fragment does not belong to this document")
        changes = {k: v for k, v in updates.items() if k in self.FRAGMENT_FIELDS}
        if not changes:
            raise ValueError("no editable fields supplied")
        if "text" in changes and not str(changes["text"] or "").strip():
            raise ValueError("fragment text cannot be empty")

        edited = {
            **payload,
            **changes,
            "manual_edited_at": datetime.now(timezone.utc).isoformat(),
        }
        if "text" in changes and changes["text"] != payload.get("text"):
            with create_embedder() as embedder:
                vectors = embedder.embed_documents([changes["text"]])
            if not vectors or len(vectors[0]) != self.settings.vector_size:
                raise ValueError(
                    "embedding service returned an unexpected vector dimension"
                )
            self.qdrant.replace_point(fragment_id, vectors[0], edited)
        else:
            self.qdrant.set_point_payload(
                fragment_id, changes | {"manual_edited_at": edited["manual_edited_at"]}
            )
        if "tags" in changes:
            record = self.registry.get_document(doc_id)
            if record:
                record["tags"] = sorted(
                    {
                        tag
                        for point in self.qdrant.list_by_doc(doc_id)
                        for tag in point.get("tags", [])
                    }
                )
                self.registry.register_document(doc_id, record)
        return {**edited, "id": fragment_id}


class TagsService:
    """Aggregate all unique tags from the fragment collection."""

    def __init__(self, qdrant: QdrantRepository) -> None:
        self.qdrant = qdrant

    def __repr__(self) -> str:
        return f"{type(self).__name__}(qdrant={type(self.qdrant).__name__})"

    def get_tags(self) -> TagsResponse:
        """Return a sorted list of all distinct tag values across the collection."""
        payloads = self.qdrant.scroll_payloads()
        tags: set[str] = set()
        for pl in payloads:
            tags.update(pl.get("tags", []) or [])
        sorted_tags = sorted(tags)
        return TagsResponse(count=len(sorted_tags), tags=sorted_tags)
