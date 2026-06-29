"""Services: IngestionService (document ingestion) and SearchService (vector search)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
)

from src.api_clients import OllamaClient
from src.common.config import Settings
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dvd_service.dto import (
    DocumentDetail,
    DocumentFragment,
    DocumentInfo,
    DocumentList,
    DocumentListResponse,
    DocumentSummary,
    NodePayload,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from src.dvd_service.modules.doc_parsers import PARSER_VERSION, DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.identity import (
    build_aliases,
    build_lookup_keys,
    make_span_id,
    make_version_id,
)
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import Tagger, VersionDetector

log = structlog.get_logger(__name__)


class IngestionService:
    def __init__(
        self,
        parser: DocumentParser,
        structure: StructureTagger,
        hierarchy: HierarchyBuilder,
        tagger: Tagger,
        version_detector: VersionDetector,
        reference_extractor: ReferenceExtractor,
        reference_resolver: ReferenceResolver,
        qdrant: QdrantRepository,
        registry: DocumentRegistry,
        jobs: JobStore,
        settings: Settings,
    ) -> None:
        self.parser = parser
        self.structure = structure
        self.hierarchy = hierarchy
        self.tagger = tagger
        self.version_detector = version_detector
        self.reference_extractor = reference_extractor
        self.reference_resolver = reference_resolver
        self.qdrant = qdrant
        self.registry = registry
        self.jobs = jobs
        self.settings = settings

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(embed_batch={self.settings.embed_batch}, "
            f"parser={type(self.parser).__name__}, "
            f"structure={type(self.structure).__name__}, "
            f"hierarchy={type(self.hierarchy).__name__}, "
            f"tagger={type(self.tagger).__name__}, "
            f"version_detector={type(self.version_detector).__name__}, "
            f"reference_extractor={type(self.reference_extractor).__name__}, "
            f"reference_resolver={type(self.reference_resolver).__name__}, "
            f"qdrant={type(self.qdrant).__name__}, "
            f"registry={type(self.registry).__name__}, "
            f"jobs={type(self.jobs).__name__})"
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

    def _embed_all(self, client: OllamaClient, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        b = self.settings.embed_batch
        for i in range(0, len(texts), b):
            vectors.extend(client.embed(texts[i : i + b]))
        return vectors

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

    def ingest(
        self,
        file_path: str,
        raw: list[dict],
        content_hash: str,
        version_override: str | None = None,
        doc_id: str | None = None,
        job_id: str | None = None,
        *,
        doc_type: str | None = None,
        corpus: str | None = None,
        lang: str | None = None,
        title: str | None = None,
        source_uri: str | None = None,
        external_ids: dict | None = None,
        metadata: dict | None = None,
        effective_date: str | None = None,
    ) -> dict:
        doc_id = doc_id or str(uuid.uuid4())
        if job_id:
            self.jobs.update(job_id, status="processing")
        client = OllamaClient()
        try:
            parts = self.parser.to_logical_parts(raw, client)  # Stage 1 + 1.5
            self.structure.tag(parts, client)  # Stage 2 + 3
            ranks = self.structure.numbering_ranks(parts)  # Stage 3.5
            tree = self.hierarchy.build(parts, ranks, title=Path(file_path).stem)
            self.hierarchy.cap_unnumbered_nesting(tree)
            self.hierarchy.group_amendment(tree)
            nodes = self.hierarchy.flatten(tree)  # prev/next, kind, html

            name, det_version = self.version_detector.detect(parts, client)
            version = (version_override or det_version).strip() or "unknown"
            version, other_versions = self._resolve_version(name, version, content_hash)

            node_tags = self.tagger.tag_nodes(nodes, client)
            uploaded_at = datetime.now(timezone.utc).isoformat()

            # Stage: extract + resolve references (links to other documents/clauses)
            numbering_index = self._numbering_index(nodes)
            node_refs: dict[str, list] = {}
            if self.settings.enable_reference_linking:
                raw_refs = self.reference_extractor.extract(nodes, client)
                node_refs = self.reference_resolver.resolve(
                    doc_id, name, raw_refs, numbering_index
                )

            vectors = self._embed_all(client, [n["text"] for n in nodes])

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
                "model": self.settings.ollama_embed_model,
                "dim": self.settings.vector_size,
                "metric": "cosine",
                "normalized": True,
            }

            points = [
                PointStruct(
                    id=n["id"],
                    vector=vec,
                    payload=NodePayload(
                        parser_version=PARSER_VERSION,
                        embedding_meta=embedding_meta,
                        doc_id=doc_id,
                        name=name,
                        title=title,
                        version=version,
                        version_id=version_id,
                        other_versions=other_versions,
                        content_hash=content_hash,
                        doc_type=doc_type,
                        corpus=corpus,
                        lang=lang,
                        external_ids=external_ids,
                        aliases=aliases,
                        lookup_keys=lookup_keys,
                        effective_date=effective_date,
                        source=os.path.basename(file_path),
                        source_uri=source_uri or os.path.basename(file_path),
                        kind=n["kind"],
                        type=n["type"],
                        numbering=n["numbering"],
                        block=n["block"],
                        depth=n["depth"],
                        order=order,
                        parent_id=n["parent_id"],
                        parent_text=n["parent_text"],
                        child_ids=n["child_ids"],
                        prev_id=n["prev_id"],
                        next_id=n["next_id"],
                        breadcrumb=n["breadcrumb"],
                        tags=node_tags.get(n["id"], []),
                        metadata=metadata or {},
                        table_html=n["table_html"],
                        references=node_refs.get(n["id"], []),
                        uploaded_at=uploaded_at,
                        text=n["text"],
                        **self._grounding(n, spans, doc_id),
                    ).model_dump(),
                )
                for order, (n, vec) in enumerate(zip(nodes, vectors))
            ]
            count = self.qdrant.upsert(points)

            # For already-loaded versions, refresh their list of other versions (including the new one)
            all_versions = set(other_versions) | {version}
            for v in other_versions:
                self.qdrant.set_other_versions(name, v, sorted(all_versions - {v}))
            self.registry.register(content_hash, name, version, doc_id)
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
                    "content_hash": content_hash,
                    "node_count": count,
                    "uploaded_at": uploaded_at,
                },
            )

            # Complete dangling references from earlier documents that pointed at this one
            if self.settings.enable_reference_linking:
                self.reference_resolver.backfill(name, doc_id, version, numbering_index)

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
            must.append(
                FieldCondition(key="version", match=MatchValue(value=req.version))
            )
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
        with OllamaClient() as client:
            vector = client.embed([req.query])[0]
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
            must.append(FieldCondition(key="version", match=MatchValue(value=version)))
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
        for pl in payloads:
            key = (pl.get("name", ""), pl.get("version", ""))
            g = groups.get(key)
            if g is None:
                g = {
                    "doc_id": pl.get("doc_id", ""),
                    "other_versions": pl.get("other_versions", []) or [],
                    "source": pl.get("source"),
                    "uploaded_at": pl.get("uploaded_at") or None,
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
            **{k: rec[k] for k in DocumentSummary.model_fields if k in rec}
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
            content_hash=pl.get("content_hash"),
            node_count=node_count,
            uploaded_at=pl.get("uploaded_at"),
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
