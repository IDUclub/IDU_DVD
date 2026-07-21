"""Unit tests for the startup housekeeping in src/dependencies/init_dependencies.

Ingestion runs in-process and never resumes, so a restart must not leave jobs looking alive.
Covers ``_abort_orphaned_jobs`` (job status + scratch-file sweep) and the registry/collection
divergence warning. Runs the real ``JobStore`` against fakeredis.
"""

from __future__ import annotations

import structlog

from src.common.config import Settings
from src.common.db.redis_client import JobStore, RedisClient
from src.dependencies.init_dependencies import (
    _abort_orphaned_jobs,
    _warn_on_registry_divergence,
)


def _jobs(fake_redis) -> JobStore:
    return JobStore(RedisClient(Settings()))


def _job(job_id: str, status: str) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "filename": f"{job_id}.docx",
        "created_at": "2026-01-01T00:00:00",
    }


class TestAbortOrphanedJobs:
    def test_queued_and_processing_jobs_are_failed(self, fake_redis, tmp_path):
        jobs = _jobs(fake_redis)
        jobs.set("a", _job("a", "queued"))
        jobs.set("b", _job("b", "processing"))
        _abort_orphaned_jobs(
            jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )
        for job_id in ("a", "b"):
            job = jobs.get(job_id)
            assert job["status"] == "error"
            assert "перезапуском" in job["error"]
        assert jobs.active() == []

    def test_finished_jobs_are_left_alone(self, fake_redis, tmp_path):
        jobs = _jobs(fake_redis)
        jobs.set("done", {**_job("done", "done"), "name": "СП 1"})
        jobs.set("failed", {**_job("failed", "error"), "error": "boom"})
        _abort_orphaned_jobs(
            jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )
        assert jobs.get("done")["status"] == "done"
        assert jobs.get("failed")["error"] == "boom"  # not overwritten

    def test_scratch_files_are_swept(self, fake_redis, tmp_path):
        (tmp_path / "job1_doc.docx").write_bytes(b"x")
        (tmp_path / "job2_doc.txt").write_bytes(b"y")
        _abort_orphaned_jobs(
            _jobs(fake_redis),
            Settings(upload_dir=str(tmp_path)),
            structlog.get_logger(),
        )
        assert list(tmp_path.iterdir()) == []

    def test_missing_upload_dir_is_not_an_error(self, fake_redis, tmp_path):
        _abort_orphaned_jobs(
            _jobs(fake_redis),
            Settings(upload_dir=str(tmp_path / "nope")),
            structlog.get_logger(),
        )

    def test_unreachable_redis_does_not_block_startup(self, tmp_path):
        class Exploding:
            def active(self):
                raise ConnectionError("redis down")

        _abort_orphaned_jobs(
            Exploding(), Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )


class TestRegistryDivergenceWarning:
    """A populated registry over an empty collection means the two stores drifted apart —
    it is reported, never repaired in bulk (a boot against the wrong Qdrant would wipe it).
    """

    class FakeQdrant:
        collection = "documents__giga_embeddings_instruct_2048"

        def __init__(self, points: int) -> None:
            self._points = points

        def count(self, query_filter=None) -> int:
            return self._points

    class FakeRegistry:
        def __init__(self, names: list[str]) -> None:
            self._names = names

        def names(self) -> list[str]:
            return self._names

    def _warnings(self, points: int, names: list[str]) -> list[str]:
        seen: list[str] = []

        class Recorder:
            def warning(self, event, **kw):
                seen.append(event)

        _warn_on_registry_divergence(
            self.FakeQdrant(points), self.FakeRegistry(names), Recorder()
        )
        return seen

    def test_warns_when_registry_describes_an_empty_collection(self):
        assert self._warnings(0, ["СП 1"]) == ["registry_diverged_from_collection"]

    def test_silent_when_both_are_populated(self):
        assert self._warnings(42, ["СП 1"]) == []

    def test_silent_on_a_fresh_install(self):
        assert self._warnings(0, []) == []
