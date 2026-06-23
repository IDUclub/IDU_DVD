"""Qdrant repository: collection setup, upsert, vector search, and retrieval by id."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import structlog
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
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
    "kind": PayloadSchemaType.KEYWORD,
    "type": PayloadSchemaType.KEYWORD,
    "block": PayloadSchemaType.KEYWORD,
    "parent_id": PayloadSchemaType.KEYWORD,
    "content_hash": PayloadSchemaType.KEYWORD,
    "tags": PayloadSchemaType.KEYWORD,
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

    def retrieve(self, ids: Sequence[str]) -> dict[str, dict]:
        if not ids:
            return {}
        recs = self.client.retrieve(self.collection, ids=list(ids), with_payload=True)
        return {str(r.id): (r.payload or {}) for r in recs}

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
