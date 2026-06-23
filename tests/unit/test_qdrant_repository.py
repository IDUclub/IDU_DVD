"""Unit tests for src/common/db/qdrant_client — QdrantRepository.

The qdrant-client is mocked, so these verify the repository's own logic: collection/index
creation, upsert counting, search/retrieve mapping, payload updates, and __repr__.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.common.db.qdrant_client import _PAYLOAD_INDEXES, QdrantRepository


@pytest.fixture
def repo_and_client(settings):
    with patch("src.common.db.qdrant_client.QdrantClient") as Client:
        client = Client.return_value
        repo = QdrantRepository(settings)
        yield repo, client


class TestEnsureCollection:
    def test_creates_collection_and_indexes_when_absent(self, repo_and_client):
        repo, client = repo_and_client
        client.collection_exists.return_value = False
        repo.ensure_collection()
        client.create_collection.assert_called_once()
        assert client.create_payload_index.call_count == len(_PAYLOAD_INDEXES)

    def test_skips_creation_when_collection_exists(self, repo_and_client):
        repo, client = repo_and_client
        client.collection_exists.return_value = True
        repo.ensure_collection()
        client.create_collection.assert_not_called()


class TestUpsert:
    def test_upsert_returns_count_and_calls_client(self, repo_and_client):
        repo, client = repo_and_client
        points = [MagicMock(), MagicMock()]
        assert repo.upsert(points) == 2
        client.upsert.assert_called_once()

    def test_upsert_empty_is_noop(self, repo_and_client):
        repo, client = repo_and_client
        assert repo.upsert([]) == 0
        client.upsert.assert_not_called()


class TestSearchAndRetrieve:
    def test_search_returns_points(self, repo_and_client):
        repo, client = repo_and_client
        client.query_points.return_value = SimpleNamespace(points=["p1", "p2"])
        assert repo.search([0.1, 0.2], None, 5) == ["p1", "p2"]

    def test_retrieve_maps_id_to_payload(self, repo_and_client):
        repo, client = repo_and_client
        client.retrieve.return_value = [
            SimpleNamespace(id="a", payload={"text": "A"}),
            SimpleNamespace(id="b", payload=None),
        ]
        assert repo.retrieve(["a", "b"]) == {"a": {"text": "A"}, "b": {}}

    def test_retrieve_empty_short_circuits(self, repo_and_client):
        repo, client = repo_and_client
        assert repo.retrieve([]) == {}
        client.retrieve.assert_not_called()


class TestSetOtherVersions:
    def test_updates_payload_for_version(self, repo_and_client):
        repo, client = repo_and_client
        repo.set_other_versions("СП 1", "v1", ["v2", "v3"])
        client.set_payload.assert_called_once()


class TestRepr:
    def test_repr_mentions_collection_and_vector_size(self, repo_and_client):
        repo, _ = repo_and_client
        r = repr(repo)
        assert "collection=documents" in r and "vector_size=1024" in r
