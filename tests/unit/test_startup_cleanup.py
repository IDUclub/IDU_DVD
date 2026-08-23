"""Unit tests for the startup housekeeping in src/dependencies/init_dependencies.

Ingestion is queued durably, so a restart must *resume* what was interrupted rather than
declare it lost — and must still fail jobs that no queue entry can account for. Covers
``_resume_interrupted_jobs`` (requeue + job status + scratch-file sweep) and the
registry/collection divergence warning. Runs the real ``JobStore``/``IngestQueue`` against
fakeredis.
"""

from __future__ import annotations

import structlog

from src.common.config import Settings
from src.common.db.redis_client import JobStore, RedisClient
from src.dependencies.init_dependencies import (
    _resume_interrupted_jobs,
    _warn_on_registry_divergence,
)
from src.dvd_service.ingest_queue import IngestQueue


def _jobs(fake_redis) -> JobStore:
    return JobStore(RedisClient(Settings()))


def _queue(fake_redis) -> IngestQueue:
    return IngestQueue(RedisClient(Settings()), Settings())


def _job(job_id: str, status: str) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "filename": f"{job_id}.docx",
        "created_at": "2026-01-01T00:00:00",
    }


def _entry(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "operation": "upload",
        "content_hash": f"hash-{job_id}",
        "source_object_key": f"hash-{job_id}.docx",
        "filename": f"{job_id}.docx",
    }


class TestResumeInterruptedJobs:
    def test_inflight_jobs_go_back_to_the_queue(self, fake_redis, tmp_path):
        jobs, queue = _jobs(fake_redis), _queue(fake_redis)
        queue.enqueue(_entry("a"))
        claimed = queue.claim()  # the process dies here
        jobs.set(claimed["job_id"], _job("a", "processing"))

        _resume_interrupted_jobs(
            queue, jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )

        assert [e["job_id"] for e in queue.pending()] == ["a"]
        assert queue.inflight() == []
        assert jobs.get("a")["status"] == "queued"

    def test_resumed_job_keeps_its_place_at_the_head(self, fake_redis, tmp_path):
        """A retry must not drift behind documents uploaded after it."""
        jobs, queue = _jobs(fake_redis), _queue(fake_redis)
        queue.enqueue(_entry("first"))
        queue.claim()
        queue.enqueue(_entry("later"))

        _resume_interrupted_jobs(
            queue, jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )

        assert [e["job_id"] for e in queue.pending()] == ["first", "later"]

    def test_jobs_without_a_queue_entry_are_failed(self, fake_redis, tmp_path):
        """Nothing describes them any more, so they cannot be resumed — say so."""
        jobs, queue = _jobs(fake_redis), _queue(fake_redis)
        jobs.set("orphan", _job("orphan", "processing"))

        _resume_interrupted_jobs(
            queue, jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )

        job = jobs.get("orphan")
        assert job["status"] == "error"
        assert "перезапуском" in job["error"]

    def test_queued_job_still_in_the_queue_is_left_alone(self, fake_redis, tmp_path):
        jobs, queue = _jobs(fake_redis), _queue(fake_redis)
        queue.enqueue(_entry("waiting"))
        jobs.set("waiting", _job("waiting", "queued"))

        _resume_interrupted_jobs(
            queue, jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )

        assert jobs.get("waiting")["status"] == "queued"

    def test_finished_jobs_are_left_alone(self, fake_redis, tmp_path):
        jobs, queue = _jobs(fake_redis), _queue(fake_redis)
        jobs.set("done", {**_job("done", "done"), "name": "СП 1"})
        jobs.set("failed", {**_job("failed", "error"), "error": "boom"})

        _resume_interrupted_jobs(
            queue, jobs, Settings(upload_dir=str(tmp_path)), structlog.get_logger()
        )

        assert jobs.get("done")["status"] == "done"
        assert jobs.get("failed")["error"] == "boom"  # not overwritten

    def test_scratch_files_are_swept(self, fake_redis, tmp_path):
        (tmp_path / "job1_doc.docx").write_bytes(b"x")
        (tmp_path / "job2_doc.txt").write_bytes(b"y")

        _resume_interrupted_jobs(
            _queue(fake_redis),
            _jobs(fake_redis),
            Settings(upload_dir=str(tmp_path)),
            structlog.get_logger(),
        )

        assert list(tmp_path.iterdir()) == []

    def test_missing_upload_dir_is_not_an_error(self, fake_redis, tmp_path):
        _resume_interrupted_jobs(
            _queue(fake_redis),
            _jobs(fake_redis),
            Settings(upload_dir=str(tmp_path / "nope")),
            structlog.get_logger(),
        )

    def test_unreachable_redis_does_not_block_startup(self, tmp_path):
        class Exploding:
            def recover(self):
                raise ConnectionError("redis down")

            def active(self):
                raise ConnectionError("redis down")

        _resume_interrupted_jobs(
            Exploding(),
            Exploding(),
            Settings(upload_dir=str(tmp_path)),
            structlog.get_logger(),
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
