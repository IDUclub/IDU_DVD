"""Fragment relations in their own Qdrant collection, next to the fragments they link.

Kept out of the fragment payload on purpose: a clause of a long regulation can depend on
dozens of others, rescoring must not rewrite points that carry vectors, and search needs the
outgoing edges of a handful of hits — one filtered scroll, not a payload walk.

Points carry a dummy 1-d vector (the same key/value convention as the learned-pattern
collection), a deterministic id per directed pair, and indexes on the fields search and
lifecycle filter by.
"""

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
    Range,
    VectorParams,
)

from src.common.config import Settings
from src.dvd_service.dto.relation import FragmentRelation

log = structlog.get_logger(__name__)

_INDEXES = {
    "source_id": PayloadSchemaType.KEYWORD,
    "target_id": PayloadSchemaType.KEYWORD,
    "doc_id": PayloadSchemaType.KEYWORD,
    "kind": PayloadSchemaType.KEYWORD,
    "weight": PayloadSchemaType.FLOAT,
}
_NAMESPACE = uuid.UUID("5b0c2f4e-8a61-4d3c-9f1e-2d7a6c4b9e10")


def relation_point_id(source_id: str, target_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{source_id}->{target_id}"))


class RelationRepository:
    def __init__(self, settings: Settings, client: QdrantClient | None = None) -> None:
        self.settings = settings
        self.collection = settings.relation_collection
        self.client = client or QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=settings.qdrant_timeout,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(collection={self.collection})"

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=1, distance=Distance.COSINE),
            )
            log.info("qdrant_relation_collection_created", name=self.collection)
        for field, schema in _INDEXES.items():
            try:
                self.client.create_payload_index(
                    self.collection, field_name=field, field_schema=schema
                )
            except Exception:  # noqa: BLE001 — index already exists
                pass

    def upsert(self, relations: Iterable[FragmentRelation]) -> int:
        """Store relations; a directed pair has one id, so rescoring overwrites in place.

        Lifecycle follows the fragments, not the document: versions of a document share its
        ``doc_id`` and a delta update shares unchanged fragments between versions, so edges
        are removed with the fragments they touch (``delete_by_fragments``).
        """
        points = [
            PointStruct(
                id=relation_point_id(r.source_id, r.target_id),
                vector=[0.0],
                payload=r.model_dump(),
            )
            for r in relations
        ]
        batch = (
            self.settings.qdrant_upsert_batch_size * 8
        )  # payload-only points are small
        for i in range(0, len(points), batch):
            self.client.upsert(self.collection, points=points[i : i + batch])
        return len(points)

    def delete_by_doc(self, doc_id: str) -> None:
        self.client.delete(
            self.collection,
            points_selector=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            ),
        )

    def delete_by_fragments(self, fragment_ids: Sequence[str]) -> None:
        """Remove every edge touching these fragments (both directions)."""
        if not fragment_ids:
            return
        ids = list(fragment_ids)
        self.client.delete(
            self.collection,
            points_selector=Filter(
                should=[
                    FieldCondition(key="source_id", match=MatchAny(any=ids)),
                    FieldCondition(key="target_id", match=MatchAny(any=ids)),
                ]
            ),
        )

    def outgoing(
        self, source_ids: Sequence[str], min_weight: float = 0.0
    ) -> list[FragmentRelation]:
        """Edges leaving ``source_ids`` with ``weight >= min_weight``, strongest first."""
        if not source_ids:
            return []
        query = Filter(
            must=[
                FieldCondition(key="source_id", match=MatchAny(any=list(source_ids))),
                FieldCondition(key="weight", range=Range(gte=min_weight)),
            ]
        )
        return sorted(self._scroll(query), key=lambda r: -r.weight)

    def by_doc(self, doc_id: str, min_weight: float = 0.0) -> list[FragmentRelation]:
        query = Filter(
            must=[
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                FieldCondition(key="weight", range=Range(gte=min_weight)),
            ]
        )
        return sorted(
            self._scroll(query), key=lambda r: (r.source_id, -r.weight, r.target_id)
        )

    def _scroll(self, query: Filter) -> list[FragmentRelation]:
        out: list[FragmentRelation] = []
        offset = None
        while True:
            recs, offset = self.client.scroll(
                self.collection,
                scroll_filter=query,
                limit=512,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            out.extend(FragmentRelation(**(r.payload or {})) for r in recs)
            if offset is None:
                return out
