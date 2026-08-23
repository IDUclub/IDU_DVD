"""Unit tests for the durable ingestion queue and the workers that drain it.

The queue is the reason an upload survives a restart, so the tests are about the guarantees it
sells: order is preserved, a job leaves the queue only once its document is indexed, an
interrupted job comes back at the head, and a job that keeps failing eventually stops taking
the service down with it.

``IngestQueue`` runs for real against fakeredis; the worker is exercised with fakes for the
pipeline, the parser and MinIO, and is driven synchronously (``_process``) so no event loop is
involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from src.common.config import Settings
from src.dvd_service.ingest_worker import IngestWorker


def _entry(job_id: str, operation: str = "upload", **extra) -> dict:
    return {
        "job_id": job_id,
        "operation": operation,
        "content_hash": f"hash-{job_id}",
        "source_object_key": f"hash-{job_id}.docx",
        "filename": f"{job_id}.docx",
        "name": None,
        "version": None,
        "meta": {},
        "doc_id": f"doc-{job_id}",
        "scope": None,
        **extra,
    }


class TestQueueOrder:
    def test_jobs_come_out_in_the_order_they_arrived(self, ingest_queue):
        for job_id in ("a", "b", "c"):
            ingest_queue.enqueue(_entry(job_id))
        assert [ingest_queue.claim()["job_id"] for _ in range(3)] == ["a", "b", "c"]

    def test_empty_queue_claims_nothing(self, ingest_queue):
        assert ingest_queue.claim() is None

    def test_enqueue_stamps_arrival_and_zero_attempts(self, ingest_queue):
        entry = ingest_queue.enqueue({"job_id": "a", "operation": "upload"})
        assert entry["attempts"] == 0
        assert entry["enqueued_at"]

    def test_a_claimed_job_is_not_handed_to_a_second_worker(self, ingest_queue):
        """The claim has to be atomic — it is what allows more than one worker."""
        ingest_queue.enqueue(_entry("only"))
        assert ingest_queue.claim()["job_id"] == "only"
        assert ingest_queue.claim() is None
        assert [e["job_id"] for e in ingest_queue.inflight()] == ["only"]


class TestCommitAndFailure:
    def test_commit_clears_the_job(self, ingest_queue):
        ingest_queue.enqueue(_entry("a"))
        ingest_queue.commit(ingest_queue.claim())
        assert ingest_queue.pending() == [] and ingest_queue.inflight() == []

    def test_failure_requeues_at_the_head(self, ingest_queue):
        """A retry must not drift behind documents uploaded after it."""
        ingest_queue.enqueue(_entry("first"))
        claimed = ingest_queue.claim()
        ingest_queue.enqueue(_entry("later"))

        dead, updated = ingest_queue.fail(claimed, "боль")

        assert dead is False and updated["attempts"] == 1
        assert [e["job_id"] for e in ingest_queue.pending()] == ["first", "later"]
        assert ingest_queue.inflight() == []

    def test_failure_records_the_reason(self, ingest_queue):
        ingest_queue.enqueue(_entry("a"))
        _, updated = ingest_queue.fail(ingest_queue.claim(), "Ollama недоступен")
        assert updated["last_error"] == "Ollama недоступен"
        assert updated["failed_at"]

    def test_job_is_dead_lettered_once_attempts_run_out(self, settings, ingest_queue):
        ingest_queue.enqueue(_entry("doomed"))
        for attempt in range(settings.ingest_max_attempts):
            claimed = ingest_queue.claim()
            dead, _ = ingest_queue.fail(claimed, "боль")
        assert dead is True
        assert ingest_queue.pending() == [] and ingest_queue.inflight() == []
        assert [e["job_id"] for e in ingest_queue.dead()] == ["doomed"]

    def test_dead_letter_keeps_the_job_out_of_the_queue(self, ingest_queue):
        """Otherwise a document that kills the process would kill it again on every boot."""
        ingest_queue.enqueue(_entry("doomed"))
        for _ in range(ingest_queue.max_attempts):
            ingest_queue.fail(ingest_queue.claim(), "боль")
        assert ingest_queue.recover() == []
        assert ingest_queue.claim() is None

    def test_corrupt_entry_is_dropped_rather_than_blocking_the_queue(
        self, ingest_queue
    ):
        ingest_queue.r.rpush(ingest_queue.key, "{not json")
        ingest_queue.enqueue(_entry("good"))
        assert ingest_queue.claim() is None  # the corrupt head, discarded
        assert ingest_queue.claim()["job_id"] == "good"


class TestRecovery:
    def test_inflight_jobs_return_to_the_head_in_order(self, ingest_queue):
        for job_id in ("a", "b"):
            ingest_queue.enqueue(_entry(job_id))
        ingest_queue.claim()
        ingest_queue.claim()
        ingest_queue.enqueue(_entry("arrived-later"))

        recovered = ingest_queue.recover()

        assert [e["job_id"] for e in recovered] == ["a", "b"]
        assert [e["job_id"] for e in ingest_queue.pending()] == [
            "a",
            "b",
            "arrived-later",
        ]
        assert ingest_queue.inflight() == []

    def test_recovery_is_a_no_op_with_nothing_in_flight(self, ingest_queue):
        ingest_queue.enqueue(_entry("waiting"))
        assert ingest_queue.recover() == []
        assert [e["job_id"] for e in ingest_queue.pending()] == ["waiting"]

    def test_interruption_counts_as_a_failed_attempt(self, ingest_queue):
        ingest_queue.enqueue(_entry("a"))
        ingest_queue.claim()
        [recovered] = ingest_queue.recover()
        assert recovered["attempts"] == 1
        assert "перезапуском" in recovered["last_error"]

    def test_a_document_that_kills_the_process_stops_crash_looping(
        self, settings, ingest_queue
    ):
        """Otherwise every boot would claim it, die, and requeue it — forever."""
        ingest_queue.enqueue(_entry("killer"))
        for _ in range(settings.ingest_max_attempts):
            ingest_queue.claim()  # a fresh process claims it…
            ingest_queue.recover()  # …and dies before committing

        assert ingest_queue.pending() == []
        assert [e["job_id"] for e in ingest_queue.dead()] == ["killer"]


class TestCheckpoints:
    def test_checkpoint_round_trip(self, ingest_queue):
        ingest_queue.set_checkpoint("a", name="СП 1", version="2020")
        assert ingest_queue.checkpoint("a") == {"name": "СП 1", "version": "2020"}

    def test_missing_checkpoint_is_empty(self, ingest_queue):
        assert ingest_queue.checkpoint("nope") == {}
        assert ingest_queue.checkpoint(None) == {}

    def test_commit_drops_the_checkpoint(self, ingest_queue):
        ingest_queue.enqueue(_entry("a"))
        claimed = ingest_queue.claim()
        ingest_queue.set_checkpoint("a", name="СП 1", version="2020")
        ingest_queue.commit(claimed)
        assert ingest_queue.checkpoint("a") == {}


class TestRequeueDead:
    def test_dead_job_goes_back_with_a_clean_slate(self, ingest_queue):
        ingest_queue.enqueue(_entry("doomed"))
        for _ in range(ingest_queue.max_attempts):
            ingest_queue.fail(ingest_queue.claim(), "боль")

        revived = ingest_queue.requeue_dead("doomed")

        assert revived["attempts"] == 0
        assert "last_error" not in revived
        assert [e["job_id"] for e in ingest_queue.pending()] == ["doomed"]
        assert ingest_queue.dead() == []

    def test_unknown_job_is_not_requeued(self, ingest_queue):
        assert ingest_queue.requeue_dead("nope") is None


# --------------------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------------------
class FakeParser:
    def extract_raw(self, path):
        return [{"text": Path(path).name, "category": "NarrativeText", "html": None}]


class FakeIngestion:
    def __init__(self, explode: Exception | None = None) -> None:
        self.explode = explode
        self.calls: list[tuple[str, tuple, dict]] = []
        self.discarded: list[dict] = []

    def _record(self, op, args, kwargs):
        self.calls.append((op, args, kwargs))
        if self.explode:
            raise self.explode
        return {"name": "СП 1", "version": "2020"}

    def ingest(self, *a, **k):
        return self._record("ingest", a, k)

    def update(self, *a, **k):
        return self._record("update", a, k)

    def reload(self, *a, **k):
        return self._record("reload", a, k)

    def ingest_direct(self, *a, **k):
        return self._record("ingest_direct", a, k)

    def reload_direct(self, *a, **k):
        return self._record("reload_direct", a, k)

    def discard_attempt(self, **kwargs):
        self.discarded.append(kwargs)
        return {"points_removed": 3}


class FakeJobs:
    def __init__(self):
        self.store = {}

    def set(self, job_id, data):
        self.store[job_id] = data

    def get(self, job_id):
        return self.store.get(job_id)

    def update(self, job_id, **fields):
        self.store.setdefault(job_id, {}).update(fields)


@pytest.fixture
def worker(tmp_path, ingest_queue, fake_document_storage):
    def build(ingestion: FakeIngestion) -> IngestWorker:
        return IngestWorker(
            "test-worker",
            settings=Settings(upload_dir=str(tmp_path)),
            queue=ingest_queue,
            jobs=FakeJobs(),
            parser=FakeParser(),
            ingestion=ingestion,
            document_storage=fake_document_storage,
            user_document_storage=fake_document_storage,
            deps=None,
        )

    return build


class TestWorkerHappyPath:
    def test_upload_is_indexed_and_committed(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"docx bytes")
        ingest_queue.enqueue(_entry("a"))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        [(op, args, kwargs)] = ingestion.calls
        assert op == "ingest"
        assert kwargs["doc_id"] == "doc-a"
        assert kwargs["source_object_key"] == "hash-a.docx"
        assert ingest_queue.inflight() == [] and ingest_queue.pending() == []

    def test_source_is_fetched_from_storage_not_from_local_disk(
        self, worker, ingest_queue, fake_document_storage, tmp_path
    ):
        """The uploading process may be long gone — the file comes back from MinIO."""
        fake_document_storage.upload("hash-a.docx", b"docx bytes")
        ingest_queue.enqueue(_entry("a"))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        raw = ingestion.calls[0][1][1]
        assert raw[0]["text"].endswith("a.docx")
        assert list(tmp_path.iterdir()) == [], "the scratch copy is cleaned up"

    def test_update_and_reload_reach_their_own_methods(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-u.docx", b"x")
        fake_document_storage.upload("hash-r.docx", b"x")
        ingest_queue.enqueue(_entry("u", "update", name="СП 1"))
        ingest_queue.enqueue(_entry("r", "reload", name="СП 1"))
        ingestion = FakeIngestion()
        w = worker(ingestion)

        w._process(ingest_queue.claim())
        w._process(ingest_queue.claim())

        assert [op for op, _, _ in ingestion.calls] == ["update", "reload"]
        assert all(args[0] == "СП 1" for _, args, _ in ingestion.calls)

    def test_direct_payload_is_read_back_and_then_dropped(
        self, worker, ingest_queue, fake_document_storage
    ):
        payload = {
            "name": "ПРЯМОЙ",
            "fragments": [{"text": "раз"}, {"text": "два"}],
        }
        fake_document_storage.upload(
            "queue/d.json", json.dumps(payload).encode("utf-8")
        )
        ingest_queue.enqueue(
            _entry(
                "d",
                "upload-direct",
                source_object_key=None,
                payload_object_key="queue/d.json",
            )
        )
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        op, args, _ = ingestion.calls[0]
        assert op == "ingest_direct"
        assert [f.text for f in args[0].fragments] == ["раз", "два"]
        assert "queue/d.json" in fake_document_storage.delete_calls


class TestJobRecordRestore:
    """A job may wait in the queue longer than its status record's Redis TTL."""

    def test_expired_status_is_rebuilt_from_the_queue_entry(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a", name="СП 1"))
        w = worker(FakeIngestion())  # its jobs store is empty — the record expired

        w._process(ingest_queue.claim())

        record = w.jobs.store["a"]
        assert record["filename"] == "a.docx"
        assert record["name"] == "СП 1"
        assert record["operation"] == "upload"

    def test_live_status_is_not_overwritten(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a"))
        w = worker(FakeIngestion())
        w.jobs.set(
            "a", {"job_id": "a", "status": "queued", "filename": "оригинал.docx"}
        )

        w._process(ingest_queue.claim())

        assert w.jobs.store["a"]["filename"] == "оригинал.docx"


class TestWorkerFailure:
    def test_failed_job_is_requeued_and_stays_claimable(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a"))
        w = worker(FakeIngestion(RuntimeError("Ollama недоступен")))

        w._process(ingest_queue.claim())

        [pending] = ingest_queue.pending()
        assert pending["attempts"] == 1
        assert "Ollama" in pending["last_error"]
        assert w.jobs.store["a"]["status"] == "queued"

    def test_job_that_keeps_failing_is_dead_lettered(
        self, worker, ingest_queue, fake_document_storage, settings
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a"))
        w = worker(FakeIngestion(RuntimeError("боль")))

        for _ in range(settings.ingest_max_attempts):
            w._process(ingest_queue.claim())

        assert [e["job_id"] for e in ingest_queue.dead()] == ["a"]
        assert w.jobs.store["a"]["status"] == "error"

    def test_missing_source_object_fails_the_job_rather_than_the_worker(
        self, worker, ingest_queue
    ):
        ingest_queue.enqueue(_entry("gone"))  # nothing was ever uploaded
        w = worker(FakeIngestion())

        w._process(ingest_queue.claim())

        assert ingest_queue.pending()[0]["attempts"] == 1


class TestRetryCleanup:
    """Node ids are regenerated on every run, so a retry that skipped cleanup would leave a
    second copy of every fragment behind."""

    def test_first_attempt_cleans_nothing(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a"))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        assert ingestion.discarded == []

    def test_upload_retry_is_cleaned_by_doc_id(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a", attempts=1))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        assert ingestion.discarded == [
            {"doc_id": "doc-a", "name": None, "version": None}
        ]

    def test_update_retry_is_cleaned_by_the_checkpointed_version(
        self, worker, ingest_queue, fake_document_storage
    ):
        """A delta update shares the base document's id — only its version isolates it."""
        fake_document_storage.upload("hash-u.docx", b"x")
        ingest_queue.enqueue(_entry("u", "update", name="СП 1", attempts=1))
        ingest_queue.set_checkpoint("u", name="СП 1", version="2020")
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        assert ingestion.discarded == [
            {"doc_id": None, "name": "СП 1", "version": "2020"}
        ]

    def test_update_retry_without_a_checkpoint_cleans_nothing(
        self, worker, ingest_queue, fake_document_storage
    ):
        """No checkpoint means the attempt died before its first write — nothing to undo."""
        fake_document_storage.upload("hash-u.docx", b"x")
        ingest_queue.enqueue(_entry("u", "update", name="СП 1", attempts=1))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        assert ingestion.discarded == [{"doc_id": None, "name": None, "version": None}]

    def test_reload_retry_needs_no_cleanup(
        self, worker, ingest_queue, fake_document_storage
    ):
        """It wipes every stored version of the name before writing anyway."""
        fake_document_storage.upload("hash-r.docx", b"x")
        ingest_queue.enqueue(_entry("r", "reload", name="СП 1", attempts=1))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        assert ingestion.discarded == []

    def test_identity_hook_records_a_checkpoint(
        self, worker, ingest_queue, fake_document_storage
    ):
        fake_document_storage.upload("hash-a.docx", b"x")
        ingest_queue.enqueue(_entry("a"))
        ingestion = FakeIngestion()

        worker(ingestion)._process(ingest_queue.claim())

        _, _, kwargs = ingestion.calls[0]
        kwargs["on_identity"]("СП 1", "2020")
        assert ingest_queue.checkpoint("a") == {"name": "СП 1", "version": "2020"}


class TestWorkerRepr:
    def test_repr_names_the_worker(self, worker):
        assert repr(worker(FakeIngestion())) == "IngestWorker(name=test-worker)"


class TestQueueRepr:
    def test_repr_mentions_the_keys(self, ingest_queue):
        r = repr(ingest_queue)
        assert "dvd:ingest:pending" in r and "max_attempts=" in r


def test_entries_survive_a_json_round_trip(ingest_queue):
    """Commit/fail find the in-flight entry by value, so re-serializing must be stable."""
    original = _entry("a", meta={"corpus": "сп", "external_ids": {"code": "1"}})
    ingest_queue.enqueue(original)
    claimed = ingest_queue.claim()
    assert json.dumps(claimed, sort_keys=True, ensure_ascii=False) == json.dumps(
        {**original, "attempts": 0, "enqueued_at": claimed["enqueued_at"]},
        sort_keys=True,
        ensure_ascii=False,
    )
    ingest_queue.commit(claimed)
    assert ingest_queue.inflight() == []


def test_worker_logs_are_structured(worker, ingest_queue, fake_document_storage):
    """Operators find a stuck document by grepping these events."""
    fake_document_storage.upload("hash-a.docx", b"x")
    ingest_queue.enqueue(_entry("a"))
    with structlog.testing.capture_logs() as logs:
        worker(FakeIngestion())._process(ingest_queue.claim())
    assert [entry["event"] for entry in logs] == ["ingest_job_done"]
