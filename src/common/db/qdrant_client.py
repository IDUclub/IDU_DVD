"""Qdrant repository: collection setup, upsert, vector search, and retrieval by id."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

import structlog
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from src.common.config import Settings

log = structlog.get_logger(__name__)

# Payload fields that require filtering -> payload indexes.
_PAYLOAD_INDEXES: dict[str, PayloadSchemaType] = {
    "doc_id": PayloadSchemaType.KEYWORD,
    "name": PayloadSchemaType.KEYWORD,
    "version": PayloadSchemaType.KEYWORD,
    "version_id": PayloadSchemaType.KEYWORD,
    "kind": PayloadSchemaType.KEYWORD,
    "type": PayloadSchemaType.KEYWORD,
    "block": PayloadSchemaType.KEYWORD,
    "parent_id": PayloadSchemaType.KEYWORD,
    "content_hash": PayloadSchemaType.KEYWORD,
    "tags": PayloadSchemaType.KEYWORD,
    "numbering": PayloadSchemaType.KEYWORD,  # resolve a clause reference to a node
    "references[].target_name": PayloadSchemaType.KEYWORD,  # find who references a document
    # general-purpose identity / corpus filters
    "doc_type": PayloadSchemaType.KEYWORD,
    "corpus": PayloadSchemaType.KEYWORD,
    "lang": PayloadSchemaType.KEYWORD,
    "lookup_keys": PayloadSchemaType.KEYWORD,
    "span_id": PayloadSchemaType.KEYWORD,
    "order": PayloadSchemaType.INTEGER,
}


class QdrantRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.collection = settings.qdrant_collection
        self.client = QdrantClient(
            url=settings.qdrant_url, api_key=settings.qdrant_api_key
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(url={self.settings.qdrant_url}, "
            f"collection={self.collection}, vector_size={self.settings.vector_size})"
        )

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=self.settings.vector_size, distance=Distance.COSINE
                ),
            )
            log.info("qdrant_collection_created", name=self.collection)
        for field, schema in _PAYLOAD_INDEXES.items():
            try:
                self.client.create_payload_index(
                    self.collection, field_name=field, field_schema=schema
                )
            except Exception:  # noqa: BLE001 — index already exists
                pass

    def upsert(self, points: Iterable[PointStruct]) -> int:
        points = list(points)
        if points:
            self.client.upsert(self.collection, points=points)
        return len(points)

    def search(self, vector: Sequence[float], query_filter: Filter | None, limit: int):
        return self.client.query_points(
            self.collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        ).points

    def scroll_payloads(
        self, query_filter: Filter | None = None, batch: int = 256
    ) -> list[dict]:
        """All payloads matching ``query_filter`` (paginated scroll until exhausted)."""
        out: list[dict] = []
        offset = None
        while True:
            recs, offset = self.client.scroll(
                self.collection,
                scroll_filter=query_filter,
                limit=batch,
                offset=offset,
                with_payload=True,
            )
            out.extend((r.payload or {}) for r in recs)
            if offset is None:
                break
        return out

    def retrieve(self, ids: Sequence[str]) -> dict[str, dict]:
        if not ids:
            return {}
        recs = self.client.retrieve(self.collection, ids=list(ids), with_payload=True)
        return {str(r.id): (r.payload or {}) for r in recs}

    def list_by_doc(self, doc_id: str, limit: int = 10000) -> list[dict]:
        """All point payloads of a document (for the document-level read API).

        The point id (the node's stable id) is injected as ``id`` since it lives on the point,
        not inside the payload.
        """
        flt = Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
        )
        points, _ = self.client.scroll(
            self.collection,
            scroll_filter=flt,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [{**(p.payload or {}), "id": str(p.id)} for p in points]

    def doc_ids_by_lookup_key(self, key: str, limit: int = 1000) -> list[str]:
        """Distinct doc_ids whose payload carries the given exact lookup key / external id."""
        flt = Filter(
            must=[FieldCondition(key="lookup_keys", match=MatchAny(any=[key]))]
        )
        points, _ = self.client.scroll(
            self.collection,
            scroll_filter=flt,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        seen: list[str] = []
        for p in points:
            did = (p.payload or {}).get("doc_id")
            if did and did not in seen:
                seen.append(did)
        return seen

    def set_other_versions(
        self, name: str, version: str, other_versions: list[str]
    ) -> None:
        """Update the other_versions field on all points of a specific document version."""
        self.client.set_payload(
            self.collection,
            payload={"other_versions": other_versions},
            points=Filter(
                must=[
                    FieldCondition(key="name", match=MatchValue(value=name)),
                    FieldCondition(key="version", match=MatchValue(value=version)),
                ]
            ),
        )

    # --- reference resolution ---
    def find_node(
        self, name: str, numbering: str = "", version: str | None = None
    ) -> dict | None:
        """Locate a node by document name (+ optional clause numbering / version).

        Returns ``{node_id, doc_id, version, numbering}`` of the best match, or ``None``. When
        several versions match, the lexicographically latest version is preferred.
        """
        must = [FieldCondition(key="name", match=MatchValue(value=name))]
        if numbering:
            must.append(
                FieldCondition(key="numbering", match=MatchValue(value=numbering))
            )
        if version:
            must.append(FieldCondition(key="version", match=MatchValue(value=version)))
        recs, _ = self.client.scroll(
            self.collection,
            scroll_filter=Filter(must=must),
            limit=32,
            with_payload=True,
        )
        if not recs:
            return None
        best = max(recs, key=lambda r: (r.payload or {}).get("version", ""))
        pl = best.payload or {}
        return {
            "node_id": str(best.id),
            "doc_id": pl.get("doc_id"),
            "version": pl.get("version"),
            "numbering": pl.get("numbering", ""),
        }

    def update_references(self, node_id: str, references: list[dict]) -> None:
        """Replace the references payload of a single node (used by reference back-fill)."""
        self.client.set_payload(
            self.collection, payload={"references": references}, points=[node_id]
        )

    # --- learned reference patterns (durable, separate collection) ---
    def ensure_pattern_collection(self) -> None:
        """Create the learned-pattern collection if absent (dummy 1-d vectors — key/value store)."""
        coll = self.settings.ref_pattern_collection
        if not self.client.collection_exists(coll):
            self.client.create_collection(
                collection_name=coll,
                vectors_config=VectorParams(size=1, distance=Distance.COSINE),
            )
            log.info("qdrant_pattern_collection_created", name=coll)

    def add_pattern(self, pattern: dict) -> str:
        """Persist a learned regex pattern; returns its point id."""
        pid = str(uuid.uuid4())
        self.client.upsert(
            self.settings.ref_pattern_collection,
            points=[PointStruct(id=pid, vector=[0.0], payload=pattern)],
        )
        return pid

    def all_patterns(self) -> list[dict]:
        """All learned patterns (scrolled out of the pattern collection)."""
        coll = self.settings.ref_pattern_collection
        out: list[dict] = []
        offset = None
        while True:
            recs, offset = self.client.scroll(
                coll, limit=256, offset=offset, with_payload=True
            )
            out.extend((r.payload or {}) for r in recs)
            if offset is None:
                break
        return out
