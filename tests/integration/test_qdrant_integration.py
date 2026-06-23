"""Integration: src/common/db/qdrant_client against a real Qdrant (Docker Compose).

Verifies collection/index creation, upsert, vector search, retrieval, and payload updates on a
throwaway collection that is dropped afterwards.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client.models import PointStruct

from src.common.db.qdrant_client import QdrantRepository

pytestmark = pytest.mark.integration


def _vec(dim: int, hot: int) -> list[float]:
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


class TestQdrantRepository:
    def test_full_roundtrip(self, temp_collection):
        repo = QdrantRepository(temp_collection)
        repo.ensure_collection()

        dim = temp_collection.vector_size
        id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
        n = repo.upsert(
            [
                PointStruct(
                    id=id1,
                    vector=_vec(dim, 0),
                    payload={"name": "Док", "version": "v1", "text": "первый"},
                ),
                PointStruct(
                    id=id2,
                    vector=_vec(dim, 1),
                    payload={"name": "Док", "version": "v1", "text": "второй"},
                ),
            ]
        )
        assert n == 2

        hits = repo.search(_vec(dim, 0), query_filter=None, limit=5)
        assert {str(h.id) for h in hits} >= {id1, id2}

        retrieved = repo.retrieve([id1])
        assert retrieved[id1]["text"] == "первый"

        # payload update must not raise
        repo.set_other_versions("Док", "v1", ["v2"])
