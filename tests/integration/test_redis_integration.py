"""Integration: src/common/db/redis_client against a real Redis (Docker Compose).

Verifies the job store and document registry round-trip through actual Redis. Keys are unique
per run and cleaned up afterwards.
"""

from __future__ import annotations

import uuid

import pytest

from src.common.db.redis_client import DocumentRegistry, JobStore

pytestmark = pytest.mark.integration


class TestJobStore:
    def test_set_update_get_roundtrip(self, require_redis):
        jobs = JobStore(require_redis)
        job_id = f"itest-{uuid.uuid4().hex[:8]}"
        try:
            jobs.set(job_id, {"job_id": job_id, "status": "queued"})
            jobs.update(job_id, status="done", nodes=3)
            got = jobs.get(job_id)
            assert got["status"] == "done" and got["nodes"] == 3
        finally:
            require_redis.r.delete(f"dvd:job:{job_id}")


class TestDocumentRegistry:
    def test_register_and_lookup(self, require_redis):
        reg = DocumentRegistry(require_redis)
        name = f"ITEST-{uuid.uuid4().hex[:8]}"
        content_hash = uuid.uuid4().hex
        try:
            reg.register(content_hash, name, f"{name} ред. 1", "doc-1")
            assert reg.has_hash(content_hash)
            assert reg.hash_info(content_hash)["name"] == name
            assert reg.versions(name) == [f"{name} ред. 1"]
        finally:
            require_redis.r.delete(f"dvd:hash:{content_hash}")
            require_redis.r.delete(f"dvd:versions:{name}")
