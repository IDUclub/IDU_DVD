"""Services: IngestionService (document ingestion) and SearchService (vector search)."""

from __future__ import annotations

import os
import uuid
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
from src.dvd_service.dto import NodePayload, SearchHit, SearchRequest, SearchResponse
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
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

    def ingest(
        self,
        file_path: str,
        raw: list[dict],
        content_hash: str,
        version_override: str | None = None,
        doc_id: str | None = None,
        job_id: str | None = None,
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

            # Stage: extract + resolve references (links to other documents/clauses)
            numbering_index = self._numbering_index(nodes)
            node_refs: dict[str, list] = {}
            if self.settings.enable_reference_linking:
                raw_refs = self.reference_extractor.extract(nodes, client)
                node_refs = self.reference_resolver.resolve(
                    doc_id, name, raw_refs, numbering_index
                )

            vectors = self._embed_all(client, [n["text"] for n in nodes])

            points = [
                PointStruct(
                    id=n["id"],
                    vector=vec,
                    payload=NodePayload(
                        doc_id=doc_id,
                        name=name,
                        version=version,
                        other_versions=other_versions,
                        content_hash=content_hash,
                        source=os.path.basename(file_path),
                        kind=n["kind"],
                        type=n["type"],
                        numbering=n["numbering"],
                        block=n["block"],
                        depth=n["depth"],
                        parent_id=n["parent_id"],
                        parent_text=n["parent_text"],
                        child_ids=n["child_ids"],
                        prev_id=n["prev_id"],
                        next_id=n["next_id"],
                        breadcrumb=n["breadcrumb"],
                        tags=node_tags.get(n["id"], []),
                        table_html=n["table_html"],
                        references=node_refs.get(n["id"], []),
                        text=n["text"],
                    ).model_dump(),
                )
                for n, vec in zip(nodes, vectors)
            ]
            count = self.qdrant.upsert(points)

            # For already-loaded versions, refresh their list of other versions (including the new one)
            all_versions = set(other_versions) | {version}
            for v in other_versions:
                self.qdrant.set_other_versions(name, v, sorted(all_versions - {v}))
            self.registry.register(content_hash, name, version, doc_id)

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
                    version=pl.get("version", ""),
                    other_versions=pl.get("other_versions", []) or [],
                    kind=pl.get("kind", "text"),
                    type=pl.get("type", ""),
                    numbering=pl.get("numbering", ""),
                    breadcrumb=pl.get("breadcrumb", ""),
                    parent_id=pl.get("parent_id"),
                    prev_id=pl.get("prev_id"),
                    next_id=pl.get("next_id"),
                    tags=pl.get("tags", []) or [],
                    references=pl.get("references", []) or [],
                    text=pl.get("text", ""),
                    context=context,
                    table_html=pl.get("table_html"),
                )
            )
        return SearchResponse(count=len(hits), hits=hits)
