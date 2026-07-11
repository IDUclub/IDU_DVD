"""Unit tests for src/common/db/redis_client — RedisClient, JobStore, DocumentRegistry.

Runs against fakeredis (no real Redis). Covers: ping, job lifecycle (set/get/update with TTL),
document registry (hash dedup + version sets), and __repr__.
"""

from __future__ import annotations

import pytest

from src.common.db.redis_client import (
    DocumentRegistry,
    JobStore,
    RedisClient,
    UserIndexRegistry,
)


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

    def test_active_returns_only_queued_and_processing_newest_first(self, client):
        jobs = JobStore(client)
        jobs.set(
            "old", {"job_id": "old", "status": "processing", "created_at": "2026-01-01"}
        )
        jobs.set(
            "new", {"job_id": "new", "status": "queued", "created_at": "2026-02-01"}
        )
        jobs.set(
            "done", {"job_id": "done", "status": "done", "created_at": "2026-03-01"}
        )
        assert [job["job_id"] for job in jobs.active()] == ["new", "old"]


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
        assert repr(DocumentRegistry(client)) == "DocumentRegistry(prefix=dvd)"

    def test_default_prefix_writes_legacy_keys(self, client):
        DocumentRegistry(client).register("h1", "Док", "v1", "d1")
        assert client.r.exists("dvd:hash:h1")

    def test_prefix_scopes_all_keys(self, client):
        reg = DocumentRegistry(client, prefix="dvd:coll_a")
        reg.register("h1", "Док", "v1", "d1")
        reg.register_document("d1", {"doc_id": "d1"})
        reg.add_pending("ГОСТ 1", {"raw": "x"})
        assert client.r.exists("dvd:coll_a:hash:h1")
        assert client.r.exists("dvd:coll_a:docs")
        assert client.r.exists("dvd:coll_a:pending_ref:ГОСТ 1")
        # nothing landed under the legacy prefix
        assert not client.r.exists("dvd:hash:h1")

    def test_two_prefixes_are_isolated(self, client):
        a = DocumentRegistry(client, prefix="dvd:coll_a")
        b = DocumentRegistry(client, prefix="dvd:coll_b")
        a.register("h1", "Док", "v1", "d1")
        # the same content hash is unseen in the other collection's registry
        assert a.has_hash("h1") is True
        assert b.has_hash("h1") is False


class TestRegistryDeletion:
    def test_remove_version(self, client):
        reg = DocumentRegistry(client)
        reg.register("h1", "Док", "v1", "d1")
        reg.register("h2", "Док", "v2", "d2")
        reg.remove_version("Док", "v1")
        assert reg.versions("Док") == ["v2"]

    def test_unregister_name_forgets_versions_and_name(self, client):
        reg = DocumentRegistry(client)
        reg.register("h1", "Док", "v1", "d1")
        reg.unregister_name("Док")
        assert reg.versions("Док") == [] and not reg.has_name("Док")

    def test_remove_hashes_all_versions(self, client):
        reg = DocumentRegistry(client)
        reg.register("h1", "Док", "v1", "d1")
        reg.register("h2", "Док", "v2", "d2")
        reg.register("h3", "Другой", "v1", "d3")
        assert reg.remove_hashes("Док") == 2
        assert not reg.has_hash("h1") and not reg.has_hash("h2")
        assert reg.has_hash("h3")  # other documents untouched

    def test_remove_hashes_single_version(self, client):
        reg = DocumentRegistry(client)
        reg.register("h1", "Док", "v1", "d1")
        reg.register("h2", "Док", "v2", "d2")
        assert reg.remove_hashes("Док", version="v2") == 1
        assert reg.has_hash("h1") and not reg.has_hash("h2")

    def test_unregister_document(self, client):
        reg = DocumentRegistry(client)
        reg.register_document("d1", {"doc_id": "d1", "name": "Док"})
        reg.unregister_document("d1")
        assert reg.get_document("d1") is None and reg.doc_ids() == []


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


class TestDocumentRegistryWipe:
    def test_wipe_removes_only_this_prefix(self, client):
        a = DocumentRegistry(client, prefix="dvd:coll_a")
        b = DocumentRegistry(client, prefix="dvd:coll_b")
        a.register("h1", "Док", "v1", "d1")
        a.register_document("d1", {"doc_id": "d1"})
        b.register("h2", "Док", "v1", "d2")

        a.wipe()

        assert a.has_hash("h1") is False
        assert a.get_document("d1") is None
        assert a.names() == []
        assert b.has_hash("h2") is True  # a different registry is untouched


class TestUserIndexRegistry:
    def test_create_then_get(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        record = reg.create("u1", "s1", "p1")
        assert record["user_id"] == "u1"
        assert record["scenario_id"] == "s1"
        assert record["project_id"] == "p1"
        assert record["parent_scenario_id"] is None
        assert record["created_at"]
        assert reg.get("u1", "s1") == record

    def test_get_missing_returns_none(self, client):
        assert UserIndexRegistry(client, prefix="dvd").get("u1", "nope") is None

    def test_create_duplicate_raises(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s1", "p1")
        with pytest.raises(ValueError):
            reg.create("u1", "s1", "p1")

    def test_get_or_create_is_idempotent(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        first = reg.get_or_create("u1", "s1", "p1")
        second = reg.get_or_create("u1", "s1", "p-ignored")
        assert first == second  # second call did not overwrite project_id

    def test_delete_removes_record_and_listing_entry(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s1", "p1")
        reg.delete("u1", "s1")
        assert reg.get("u1", "s1") is None
        assert reg.list_for_user("u1") == []

    def test_list_for_user_sorted_and_scoped(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s2", "p1")
        reg.create("u1", "s1", "p1")
        reg.create("u2", "s1", "p1")  # different user, must not leak in
        assert [r["scenario_id"] for r in reg.list_for_user("u1")] == ["s1", "s2"]

    def test_ancestor_chain_single_scenario(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s1", "p1")
        assert reg.ancestor_chain("u1", "s1") == ["s1"]

    def test_ancestor_chain_follows_parent_link(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s1", "p1")
        reg.create("u1", "s2", "p1", parent_scenario_id="s1")
        reg.create("u1", "s3", "p1", parent_scenario_id="s2")
        assert reg.ancestor_chain("u1", "s3") == ["s3", "s2", "s1"]

    def test_ancestor_chain_includes_declared_parent_even_without_its_own_record(
        self, client
    ):
        # The declared parent id is still a valid scenario_id to match documents against
        # even if its own index metadata is gone (e.g. deleted) — the chain just can't
        # walk any further past it, since there's no record to find a grandparent from.
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s2", "p1", parent_scenario_id="s1")  # s1 never created
        assert reg.ancestor_chain("u1", "s2") == ["s2", "s1"]

    def test_ancestor_chain_of_unknown_scenario_is_itself(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        assert reg.ancestor_chain("u1", "ghost") == ["ghost"]

    def test_create_rejects_direct_cycle(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        with pytest.raises(ValueError):
            reg.create("u1", "s1", "p1", parent_scenario_id="s1")

    def test_create_rejects_transitive_cycle(self, client):
        reg = UserIndexRegistry(client, prefix="dvd")
        reg.create("u1", "s1", "p1", parent_scenario_id="s3")
        reg.create("u1", "s2", "p1", parent_scenario_id="s1")
        # s3 -> s1 -> s2, so s3 inheriting from s2 would close the loop
        with pytest.raises(ValueError):
            reg.create("u1", "s3", "p1", parent_scenario_id="s2")

    def test_repr(self, client):
        assert (
            repr(UserIndexRegistry(client, prefix="dvd"))
            == "UserIndexRegistry(prefix=dvd)"
        )
