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
        # namespaced name already encodes the dimension -> no runtime dimension probe
        client.get_collection.assert_not_called()

    def test_namespaced_collection_name_encodes_model_and_dim(self, repo_and_client):
        repo, _ = repo_and_client
        assert repo.collection == "documents__giga_embeddings_instruct_2048"

    def test_fixed_mode_raises_on_dimension_mismatch(self, settings):
        s = settings.model_copy(
            update={"collection_namespacing": False, "vector_size": 2048}
        )
        with patch("src.common.db.qdrant_client.QdrantClient") as Client:
            client = Client.return_value
            client.collection_exists.return_value = True
            client.get_collection.return_value = SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(vectors=SimpleNamespace(size=1024))
                )
            )
            repo = QdrantRepository(s)
            with pytest.raises(RuntimeError, match="vector size 1024"):
                repo.ensure_collection()


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


class TestScrollPayloads:
    def test_scrolls_until_exhausted(self, repo_and_client):
        repo, client = repo_and_client
        client.scroll.side_effect = [
            ([SimpleNamespace(payload={"name": "a"})], "offset1"),
            ([SimpleNamespace(payload={"name": "b"})], None),
        ]
        assert repo.scroll_payloads(None) == [{"name": "a"}, {"name": "b"}]

    def test_missing_payload_becomes_empty_dict(self, repo_and_client):
        repo, client = repo_and_client
        client.scroll.return_value = ([SimpleNamespace(payload=None)], None)
        assert repo.scroll_payloads(None) == [{}]


class TestSetOtherVersions:
    def test_updates_payload_for_version(self, repo_and_client):
        repo, client = repo_and_client
        repo.set_other_versions("СП 1", "v1", ["v2", "v3"])
        client.set_payload.assert_called_once()


class TestFindNode:
    def test_returns_best_match_by_latest_version(self, repo_and_client):
        repo, client = repo_and_client
        client.scroll.return_value = (
            [
                SimpleNamespace(
                    id="n1",
                    payload={"doc_id": "d", "version": "v1", "numbering": "7.5"},
                ),
                SimpleNamespace(
                    id="n2",
                    payload={"doc_id": "d", "version": "v2", "numbering": "7.5"},
                ),
            ],
            None,
        )
        got = repo.find_node("СП 42.13330.2016", "7.5")
        assert got["node_id"] == "n2" and got["version"] == "v2"

    def test_returns_none_when_absent(self, repo_and_client):
        repo, client = repo_and_client
        client.scroll.return_value = ([], None)
        assert repo.find_node("СП X", "1") is None


class TestUpdateReferences:
    def test_sets_references_payload_on_node(self, repo_and_client):
        repo, client = repo_and_client
        repo.update_references("node-1", [{"raw": "СП 1", "resolved": True}])
        client.set_payload.assert_called_once()
        kwargs = client.set_payload.call_args.kwargs
        assert kwargs["payload"] == {"references": [{"raw": "СП 1", "resolved": True}]}
        assert kwargs["points"] == ["node-1"]


class TestPatternCollection:
    def test_ensure_creates_when_absent(self, repo_and_client):
        repo, client = repo_and_client
        client.collection_exists.return_value = False
        repo.ensure_pattern_collection()
        client.create_collection.assert_called_once()

    def test_add_pattern_upserts_and_returns_id(self, repo_and_client):
        repo, client = repo_and_client
        pid = repo.add_pattern({"regex": "x", "source": "learned"})
        assert isinstance(pid, str) and pid
        client.upsert.assert_called_once()

    def test_all_patterns_scrolls_until_exhausted(self, repo_and_client):
        repo, client = repo_and_client
        client.scroll.side_effect = [
            ([SimpleNamespace(payload={"regex": "a"})], "offset1"),
            ([SimpleNamespace(payload={"regex": "b"})], None),
        ]
        assert repo.all_patterns() == [{"regex": "a"}, {"regex": "b"}]


class TestRepr:
    def test_repr_mentions_collection_and_vector_size(self, repo_and_client):
        repo, _ = repo_and_client
        r = repr(repo)
        assert "collection=documents" in r and "vector_size=2048" in r
