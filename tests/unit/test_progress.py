"""Unit tests for src/dvd_service/modules/progress — the ingestion progress reporter.

Covers: stage indexing, in-stage counters written to the JobStore, and the no-op path when
there is no job to report against (progress must never break the pipeline).
"""

from __future__ import annotations

from src.common.db.redis_client import JobStore, RedisClient
from src.dvd_service.modules.progress import Progress


class TestProgress:
    def test_stage_increments_index_and_writes_status(self, settings, fake_redis):
        jobs = JobStore(RedisClient(settings))
        jobs.set("j", {"job_id": "j", "status": "processing"})
        p = Progress(jobs, "j", total_stages=8)

        p.stage("structure-markup")
        p.stage("embeddings", items=4)
        p.advance(3, 4)

        data = jobs.get("j")
        assert data["stage"] == "embeddings"
        assert data["stage_index"] == 2
        assert data["stage_total"] == 8
        assert data["progress"] == 3
        assert data["progress_total"] == 4
        assert data["status"] == "processing"  # progress updates never clobber status

    def test_noop_without_job(self):
        # No JobStore / job_id -> silently does nothing (unit tests calling the pipeline direct).
        p = Progress(None, None, total_stages=8)
        p.stage("x")
        p.advance(1, 2)  # must not raise
