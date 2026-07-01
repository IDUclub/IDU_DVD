"""Integration: TagsService.get_tags() and document_names search filter on a real Qdrant.

Uses only Qdrant (no Ollama, no Redis): points are upserted with hand-crafted vectors so
no embedding model is needed. Tests that:
  - scroll_payloads() correctly aggregates tags across all collection points;
  - get_tags() returns a sorted, deduplicated list;
  - the document_names MatchAny filter is honoured by the real Qdrant search engine.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client.models import FieldCondition, Filter, MatchAny, PointStruct

from src.common.db.qdrant_client import QdrantRepository
from src.dvd_service.services.dvd_service import TagsService

pytestmark = pytest.mark.integration

DIM = 8  # small fixed dimension for test vectors


def _vec(hot: int) -> list[float]:
    v = [0.0] * DIM
    v[hot % DIM] = 1.0
    return v


def _settings_with_dim(base_settings):
    return base_settings.model_copy(update={"vector_size": DIM})


class TestTagsServiceIntegration:
    def test_get_tags_aggregates_across_collection(self, temp_collection, require_qdrant):
        s = _settings_with_dim(temp_collection)
        repo = QdrantRepository(s)
        repo.ensure_collection()

        repo.upsert(
            [
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=_vec(0),
                    payload={"name": "СП 1", "version": "v1", "tags": ["пожарная безопасность", "строительство"], "text": "а"},
                ),
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=_vec(1),
                    payload={"name": "СП 1", "version": "v1", "tags": ["строительство", "нагрузки"], "text": "б"},
                ),
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=_vec(2),
                    payload={"name": "СП 2", "version": "v1", "tags": ["нагрузки", "грунты"], "text": "в"},
                ),
            ]
        )

        svc = TagsService(repo)
        resp = svc.get_tags()

        assert resp.count == 4
        assert resp.tags == sorted({"пожарная безопасность", "строительство", "нагрузки", "грунты"})

    def test_get_tags_empty_collection_returns_no_tags(self, temp_collection, require_qdrant):
        s = _settings_with_dim(temp_collection)
        repo = QdrantRepository(s)
        repo.ensure_collection()

        svc = TagsService(repo)
        resp = svc.get_tags()

        assert resp.count == 0 and resp.tags == []

    def test_get_tags_handles_fragments_with_no_tags_field(self, temp_collection, require_qdrant):
        s = _settings_with_dim(temp_collection)
        repo = QdrantRepository(s)
        repo.ensure_collection()

        repo.upsert(
            [
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=_vec(0),
                    payload={"name": "СП 1", "version": "v1", "text": "без тегов"},
                ),
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=_vec(1),
                    payload={"name": "СП 1", "version": "v1", "tags": ["климат"], "text": "с тегом"},
                ),
            ]
        )

        svc = TagsService(repo)
        resp = svc.get_tags()

        assert resp.tags == ["климат"] and resp.count == 1


class TestDocumentNamesFilterIntegration:
    def test_document_names_filter_restricts_search_results(self, temp_collection, require_qdrant):
        s = _settings_with_dim(temp_collection)
        repo = QdrantRepository(s)
        repo.ensure_collection()

        id_sp1 = str(uuid.uuid4())
        id_sp2 = str(uuid.uuid4())
        query_vec = _vec(0)

        repo.upsert(
            [
                PointStruct(
                    id=id_sp1,
                    vector=query_vec,
                    payload={"name": "СП 1", "version": "v1", "kind": "text", "text": "первый"},
                ),
                PointStruct(
                    id=id_sp2,
                    vector=_vec(1),
                    payload={"name": "СП 2", "version": "v1", "kind": "text", "text": "второй"},
                ),
            ]
        )

        flt = Filter(
            must=[FieldCondition(key="name", match=MatchAny(any=["СП 1"]))]
        )
        hits = repo.search(query_vec, flt, limit=10)

        assert len(hits) == 1 and str(hits[0].id) == id_sp1

    def test_document_names_filter_with_multiple_names(self, temp_collection, require_qdrant):
        s = _settings_with_dim(temp_collection)
        repo = QdrantRepository(s)
        repo.ensure_collection()

        id_sp1 = str(uuid.uuid4())
        id_sp2 = str(uuid.uuid4())
        id_sp3 = str(uuid.uuid4())

        repo.upsert(
            [
                PointStruct(id=id_sp1, vector=_vec(0), payload={"name": "СП 1", "text": "a"}),
                PointStruct(id=id_sp2, vector=_vec(1), payload={"name": "СП 2", "text": "б"}),
                PointStruct(id=id_sp3, vector=_vec(2), payload={"name": "СП 3", "text": "в"}),
            ]
        )

        flt = Filter(
            must=[FieldCondition(key="name", match=MatchAny(any=["СП 1", "СП 3"]))]
        )
        hits = repo.search(_vec(0), flt, limit=10)

        returned_ids = {str(h.id) for h in hits}
        assert returned_ids == {id_sp1, id_sp3}
        assert id_sp2 not in returned_ids
