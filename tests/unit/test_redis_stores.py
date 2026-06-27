"""Unit tests for src/common/db/redis_client — RedisClient, JobStore, DocumentRegistry.

Runs against fakeredis (no real Redis). Covers: ping, job lifecycle (set/get/update with TTL),
document registry (hash dedup + version sets), and __repr__.
"""

from __future__ import annotations

import pytest

from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient


@pytest.fixture
def client(settings, fake_redis):
    return RedisClient(settings)


class TestRedisClient:
    def test_ping_ok(self, client):
        assert client.ping() is True

    def test_repr(self, client):
        r = repr(client)
        assert r.startswith("RedisClient(") and "job_ttl=" in r


class TestJobStore:
    def test_set_then_get_roundtrip(self, client):
        jobs = JobStore(client)
        jobs.set("job1", {"job_id": "job1", "status": "queued"})
        assert jobs.get("job1") == {"job_id": "job1", "status": "queued"}

    def test_get_missing_returns_none(self, client):
        assert JobStore(client).get("nope") is None

    def test_update_merges_fields(self, client):
        jobs = JobStore(client)
        jobs.set("job1", {"job_id": "job1", "status": "queued"})
        jobs.update("job1", status="done", nodes=5)
        got = jobs.get("job1")
        assert got["status"] == "done" and got["nodes"] == 5

    def test_update_creates_when_absent(self, client):
        jobs = JobStore(client)
        jobs.update("fresh", status="processing")
        assert jobs.get("fresh") == {"job_id": "fresh", "status": "processing"}

    def test_repr(self, client):
        assert "ttl=" in repr(JobStore(client))


class TestDocumentRegistry:
    def test_register_sets_hash_and_version(self, client):
        reg = DocumentRegistry(client)
        reg.register("hashA", "СП 1", "СП 1 ред. 1", "doc-1")
        assert reg.has_hash("hashA") is True
        assert reg.hash_info("hashA") == {
            "name": "СП 1",
            "version": "СП 1 ред. 1",
            "doc_id": "doc-1",
        }
        assert reg.versions("СП 1") == ["СП 1 ред. 1"]
        assert reg.version_exists(
            "СП 1", "СП 1 ред. 1"
        )  # truthy (redis returns 1/True)

    def test_unknown_hash_and_version(self, client):
        reg = DocumentRegistry(client)
        assert reg.has_hash("missing") is False
        assert reg.hash_info("missing") is None
        assert reg.versions("nope") == []

    def test_versions_are_sorted_and_unique(self, client):
        reg = DocumentRegistry(client)
        reg.register("h1", "Док", "v2", "d1")
        reg.register("h2", "Док", "v1", "d2")
        reg.register("h3", "Док", "v1", "d3")  # duplicate version
        assert reg.versions("Док") == ["v1", "v2"]

    def test_register_tracks_names(self, client):
        reg = DocumentRegistry(client)
        reg.register("h1", "СП 1", "v1", "d1")
        reg.register("h2", "ГОСТ 2", "v1", "d2")
        assert reg.names() == ["ГОСТ 2", "СП 1"]
        assert reg.has_name("СП 1") and not reg.has_name("СП 3")

    def test_repr(self, client):
        assert repr(DocumentRegistry(client)) == "DocumentRegistry()"


class TestPendingReferences:
    def test_add_peek_pop_roundtrip(self, client):
        reg = DocumentRegistry(client)
        entry = {
            "source_doc_id": "d",
            "source_node_id": "n",
            "raw": "ГОСТ 9999",
            "target_numbering": "5.1",
        }
        reg.add_pending("ГОСТ 9999", entry)
        reg.add_pending("ГОСТ 9999", {**entry, "source_node_id": "n2"})

        peeked = reg.peek_pending("ГОСТ 9999")
        assert len(peeked) == 2 and peeked[0]["source_node_id"] == "n"
        # peek does not consume
        assert len(reg.peek_pending("ГОСТ 9999")) == 2

        popped = reg.pop_pending("ГОСТ 9999")
        assert len(popped) == 2
        assert reg.peek_pending("ГОСТ 9999") == []  # drained

    def test_pop_missing_returns_empty(self, client):
        assert DocumentRegistry(client).pop_pending("nope") == []
