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
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PayloadField,
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
    "versions": PayloadSchemaType.KEYWORD,  # multi-valued version tags (delta updates)
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
    # user-scoped document index (None for the shared/regular corpus)
    "user_id": PayloadSchemaType.KEYWORD,
    "project_id": PayloadSchemaType.KEYWORD,
    "scenario_id": PayloadSchemaType.KEYWORD,
}


def shared_only_condition() -> IsEmptyCondition:
    """Match points with no ``user_id`` (absent or empty) — the shared/regular document corpus.

    Applied by default wherever a caller does not opt into user-scoped search/listing, so
    user-uploaded documents never leak into unscoped results now that they share the collection.
    """
    return IsEmptyCondition(is_empty=PayloadField(key="user_id"))


def user_scope_conditions(
    user_id: str, scenario_ids: Sequence[str]
) -> list[FieldCondition]:
    """``[user_id == user_id, scenario_id in scenario_ids]`` — a user index's isolation key."""
    return [
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        FieldCondition(key="scenario_id", match=MatchAny(any=list(scenario_ids))),
    ]


class QdrantRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.collection = settings.effective_collection
        self.client = QdrantClient(
            url=settings.qdrant_url, api_key=settings.qdrant_api_key
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(url={self.settings.qdrant_url}, "
            f"collection={self.collection}, vector_size={self.settings.vector_size})"
        )

    def ensure_collection(self) -> None:
        if self.client.collection_exists(self.collection):
            # Namespacing encodes the dimension in the name, so an existing collection is
            # guaranteed to match. In fixed mode the name is reused across configs, so guard
            # against silently writing vectors of the wrong size into it.
            if not self.settings.collection_namespacing:
                self._assert_dimension_matches()
        else:
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

    def _assert_dimension_matches(self) -> None:
        """Fail fast if an existing (fixed-name) collection has a different vector size."""
        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        size = getattr(vectors, "size", None)  # unnamed single-vector collection
        if size is not None and size != self.settings.vector_size:
            raise RuntimeError(
                f"Qdrant collection '{self.collection}' has vector size {size}, but the "
                f"configured embedding dimension is {self.settings.vector_size}. Re-index "
                f"the collection, or keep DVD_COLLECTION_NAMESPACING enabled to provision a "
                f"separate space per embedding model."
            )

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

    def get_point(self, point_id: str) -> tuple[list[float], dict] | None:
        """Return one point with its vector and payload for safe manual editing."""
        recs = self.client.retrieve(
            self.collection, ids=[point_id], with_payload=True, with_vectors=True
        )
        if not recs:
            return None
        rec = recs[0]
        vector = rec.vector
        if isinstance(vector, dict):
            raise ValueError("named vectors are not supported by the document editor")
        return list(vector or []), rec.payload or {}

    def replace_point(
        self, point_id: str, vector: Sequence[float], payload: dict
    ) -> None:
        """Replace a point after text and its embedding have been edited together."""
        self.client.upsert(
            self.collection,
            points=[PointStruct(id=point_id, vector=list(vector), payload=payload)],
        )

    def set_point_payload(self, point_id: str, payload: dict) -> None:
        self.client.set_payload(self.collection, payload=payload, points=[point_id])

    def set_document_payload(self, doc_id: str, payload: dict) -> None:
        self.client.set_payload(
            self.collection,
            payload=payload,
            points=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            ),
        )

    def list_by_doc(
        self,
        doc_id: str,
        limit: int = 10000,
        extra_must: list[FieldCondition] | None = None,
    ) -> list[dict]:
        """All point payloads of a document (for the document-level read API).

        The point id (the node's stable id) is injected as ``id`` since it lives on the point,
        not inside the payload. ``extra_must`` narrows the match further (e.g. user-index scoping).
        """
        flt = Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            + (extra_must or [])
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

    def points_by_name(
        self, name: str, extra_must: list[FieldCondition] | None = None
    ) -> list[dict]:
        """All point payloads of a document by name, each with its point ``id`` injected.

        ``extra_must`` narrows the match further (e.g. user-index scoping).
        """
        flt = Filter(
            must=[FieldCondition(key="name", match=MatchValue(value=name))]
            + (extra_must or [])
        )
        out: list[dict] = []
        offset = None
        while True:
            recs, offset = self.client.scroll(
                self.collection,
                scroll_filter=flt,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            out.extend({**(r.payload or {}), "id": str(r.id)} for r in recs)
            if offset is None:
                break
        return out

    def set_versions(self, point_ids: Sequence[str], versions: list[str]) -> None:
        """Replace the multi-valued ``versions`` tags on the given points."""
        if point_ids:
            self.client.set_payload(
                self.collection, payload={"versions": versions}, points=list(point_ids)
            )

    def delete_points(self, point_ids: Sequence[str]) -> None:
        if point_ids:
            self.client.delete(self.collection, points_selector=list(point_ids))

    def delete_by_name(
        self, name: str, extra_must: list[FieldCondition] | None = None
    ) -> None:
        """Delete every point of a document (all its versions).

        ``extra_must`` narrows the match further (e.g. user-index scoping).
        """
        self.client.delete(
            self.collection,
            points_selector=Filter(
                must=[FieldCondition(key="name", match=MatchValue(value=name))]
                + (extra_must or [])
            ),
        )

    def delete_by_filter(self, query_filter: Filter) -> None:
        """Delete every point matching an arbitrary filter (e.g. wiping a whole user index)."""
        self.client.delete(self.collection, points_selector=query_filter)

    def count(self, query_filter: Filter | None = None) -> int:
        return self.client.count(
            self.collection, count_filter=query_filter, exact=True
        ).count

    def set_other_versions(
        self,
        name: str,
        version: str,
        other_versions: list[str],
        extra_must: list[FieldCondition] | None = None,
    ) -> None:
        """Update the other_versions field on all points of a specific document version.

        ``extra_must`` narrows the match further (e.g. user-index scoping).
        """
        self.client.set_payload(
            self.collection,
            payload={"other_versions": other_versions},
            points=Filter(
                must=[
                    FieldCondition(key="name", match=MatchValue(value=name)),
                    FieldCondition(key="version", match=MatchValue(value=version)),
                ]
                + (extra_must or [])
            ),
        )

    # --- reference resolution ---
    def find_node(
        self,
        name: str,
        numbering: str = "",
        version: str | None = None,
        extra_must: list[FieldCondition] | None = None,
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
        must.extend(extra_must or [])
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


class ScopedQdrantRepository:
    """Write-path wrapper restricting ``IngestionService`` to one user document index.

    Stamps ``user_id``/``project_id``/``scenario_id`` onto every upserted point and narrows every
    name/doc_id-based lookup or mutation to that exact ``(user_id, scenario_id)`` pair — never the
    inheritance chain, so a write can never touch a parent scenario's data. Implements exactly the
    subset of :class:`QdrantRepository`'s interface that :class:`IngestionService` calls, so it can
    be swapped in without any change to the ingestion pipeline itself.
    """

    def __init__(
        self,
        inner: QdrantRepository,
        *,
        user_id: str,
        project_id: str,
        scenario_id: str,
    ) -> None:
        self._inner = inner
        self.collection = inner.collection
        self.settings = inner.settings
        self._stamp = {
            "user_id": user_id,
            "project_id": project_id,
            "scenario_id": scenario_id,
        }
        self._scope_must = user_scope_conditions(user_id, [scenario_id])

    def __repr__(self) -> str:
        return f"{type(self).__name__}(inner={self._inner!r}, scope={self._stamp})"

    def upsert(self, points: Iterable[PointStruct]) -> int:
        points = list(points)
        for p in points:
            p.payload = {**(p.payload or {}), **self._stamp}
        return self._inner.upsert(points)

    def points_by_name(self, name: str) -> list[dict]:
        return self._inner.points_by_name(name, extra_must=self._scope_must)

    def list_by_doc(self, doc_id: str, limit: int = 10000) -> list[dict]:
        return self._inner.list_by_doc(doc_id, limit=limit, extra_must=self._scope_must)

    def delete_by_name(self, name: str) -> None:
        self._inner.delete_by_name(name, extra_must=self._scope_must)

    def set_other_versions(
        self, name: str, version: str, other_versions: list[str]
    ) -> None:
        self._inner.set_other_versions(
            name, version, other_versions, extra_must=self._scope_must
        )

    def find_node(
        self, name: str, numbering: str = "", version: str | None = None
    ) -> dict | None:
        return self._inner.find_node(
            name, numbering, version, extra_must=self._scope_must
        )

    # --- point-id-targeted operations: already scoped by construction, pass straight through ---
    def set_versions(self, point_ids: Sequence[str], versions: list[str]) -> None:
        self._inner.set_versions(point_ids, versions)

    def delete_points(self, point_ids: Sequence[str]) -> None:
        self._inner.delete_points(point_ids)

    def retrieve(self, ids: Sequence[str]) -> dict[str, dict]:
        return self._inner.retrieve(ids)
