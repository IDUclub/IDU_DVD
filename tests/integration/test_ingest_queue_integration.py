"""Integration tests for the durable ingestion queue against the live stack.

The unit tests prove the queue's logic against fakeredis; these prove the promise it exists
for — that a document survives the process that accepted it — against the real Redis and MinIO
that production uses. A restart is simulated the only honest way available in-process: the
queue and the worker are *rebuilt from scratch*, as a fresh boot would, and the previous
objects are dropped without ever committing their work.

The direct-ingestion path carries the end-to-end case: it needs an embedder but no LLM, so a
full upload → queue → crash → resume → indexed cycle runs whenever the configured embeddings
provider is up.
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.common.db.minio_client import DocumentStorage
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
from src.dvd_service.dto import DirectDocumentIn
from src.dvd_service.ingest_queue import IngestQueue
from src.dvd_service.ingest_worker import IngestWorker
from src.dvd_service.modules.doc_parsers import DocumentParser

pytestmark = pytest.mark.integration


@pytest.fixture
def queue_settings(live_settings):
    """Settings on their own Redis key space, so a run never touches real queued work."""
    unique = uuid.uuid4().hex[:8]
    return live_settings.model_copy(
        update={
            "ingest_queue_key": f"itest:{unique}:pending",
            "ingest_inflight_key": f"itest:{unique}:inflight",
            "ingest_dead_key": f"itest:{unique}:dead",
            "ingest_checkpoint_prefix": f"itest:{unique}:checkpoint",
        }
    )


@pytest.fixture
def live_queue(queue_settings, require_redis):
    """A queue on the real Redis; its keys are wiped afterwards."""
    client = RedisClient(queue_settings)
    queue = IngestQueue(client, queue_settings)
    yield queue
    for key in (queue.key, queue.inflight_key, queue.dead_key):
        client.r.delete(key)
    for key in client.r.scan_iter(match=f"{queue.checkpoint_prefix}:*"):
        client.r.delete(key)


@pytest.fixture
def live_storage(queue_settings):
    """The real MinIO bucket used for uploads; uploaded objects are removed afterwards."""
    from minio import Minio

    client = Minio(
        queue_settings.minio_endpoint,
        access_key=queue_settings.minio_access_key,
        secret_key=queue_settings.minio_secret_key,
        secure=queue_settings.minio_secure,
    )
    storage = DocumentStorage(client, queue_settings.minio_bucket_documents)
    try:
        storage.ensure_bucket()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MinIO unavailable on the local stack: {exc}")
    written: list[str] = []
    original_upload = storage.upload

    def tracking_upload(key, data, content_type="application/octet-stream"):
        written.append(key)
        return original_upload(key, data, content_type)

    storage.upload = tracking_upload
    yield storage
    for key in written:
        storage.delete(key)


def _entry(job_id: str, **extra) -> dict:
    return {
        "job_id": job_id,
        "operation": "upload",
        "content_hash": f"hash-{job_id}",
        "source_object_key": f"itest/{job_id}.docx",
        "filename": f"{job_id}.docx",
        "name": None,
        "version": None,
        "meta": {},
        "doc_id": f"doc-{job_id}",
        "scope": None,
        **extra,
    }


class TestQueueOnRealRedis:
    def test_queue_outlives_the_object_that_created_it(
        self, queue_settings, live_queue, require_redis
    ):
        """The state is in Redis, not in the process: a fresh IngestQueue sees the same jobs."""
        live_queue.enqueue(_entry("a"))
        live_queue.enqueue(_entry("b"))

        reborn = IngestQueue(RedisClient(queue_settings), queue_settings)

        assert [e["job_id"] for e in reborn.pending()] == ["a", "b"]
        assert reborn.claim()["job_id"] == "a"

    def test_interrupted_job_is_resumed_by_the_next_process(
        self, queue_settings, live_queue, require_redis
    ):
        live_queue.enqueue(_entry("interrupted"))
        live_queue.claim()  # a worker takes it…
        del live_queue  # …and its process dies without committing

        reborn = IngestQueue(RedisClient(queue_settings), queue_settings)
        resumed = reborn.recover()

        assert [e["job_id"] for e in resumed] == ["interrupted"]
        assert [e["job_id"] for e in reborn.pending()] == ["interrupted"]

    def test_committed_job_is_not_resurrected(
        self, queue_settings, live_queue, require_redis
    ):
        live_queue.enqueue(_entry("finished"))
        live_queue.commit(live_queue.claim())

        reborn = IngestQueue(RedisClient(queue_settings), queue_settings)

        assert reborn.recover() == [] and reborn.pending() == []

    def test_two_workers_never_take_the_same_job(
        self, queue_settings, live_queue, require_redis
    ):
        """Real Redis, not fakeredis: LMOVE is what makes ingest_concurrency > 1 safe."""
        for i in range(4):
            live_queue.enqueue(_entry(f"j{i}"))
        other = IngestQueue(RedisClient(queue_settings), queue_settings)

        taken = []
        while True:
            claimed = live_queue.claim() or other.claim()
            if claimed is None:
                break
            taken.append(claimed["job_id"])

        assert sorted(taken) == ["j0", "j1", "j2", "j3"]
        assert len(taken) == len(set(taken))


class TestSourceSurvivesInMinio:
    def test_worker_rebuilds_the_document_from_stored_bytes(
        self, queue_settings, live_queue, live_storage, require_redis
    ):
        """The uploading process is gone; the file comes back from object storage."""
        key = f"itest/{uuid.uuid4().hex}.docx"
        live_storage.upload(key, b"stored bytes", "application/octet-stream")
        live_queue.enqueue(_entry("restored", source_object_key=key))

        class RecordingParser:
            def extract_raw(self, path):
                return [{"text": open(path, "rb").read().decode(), "category": "x"}]

        class CapturingIngestion:
            def __init__(self):
                self.raw = None

            def ingest(self, path, raw, content_hash, **kwargs):
                self.raw = raw
                return {}

            def discard_attempt(self, **kwargs):
                return {}

        ingestion = CapturingIngestion()
        worker = IngestWorker(
            "itest",
            settings=queue_settings,
            queue=live_queue,
            jobs=JobStore(RedisClient(queue_settings)),
            parser=RecordingParser(),
            ingestion=ingestion,
            document_storage=live_storage,
            user_document_storage=live_storage,
            deps=None,
        )

        worker._process(live_queue.claim())

        assert ingestion.raw == [{"text": "stored bytes", "category": "x"}]
        assert live_queue.pending() == [] and live_queue.inflight() == []


class TestEndToEndCrashAndResume:
    """A real document through the real queue, MinIO, embedder and Qdrant — interrupted once."""

    @pytest.fixture
    def ingestion(self, temp_collection, require_embedder, require_redis):
        from src.common.db.qdrant_client import QdrantRepository
        from src.dvd_service.services.dvd_service import IngestionService

        qdrant = QdrantRepository(temp_collection)
        qdrant.ensure_collection()
        redis = RedisClient(temp_collection)
        registry = DocumentRegistry(redis, prefix=temp_collection.registry_prefix)
        return IngestionService(
            parser=DocumentParser(temp_collection),
            structure=None,
            hierarchy=None,
            version_detector=None,
            reference_extractor=None,
            reference_resolver=None,
            qdrant=qdrant,
            registry=registry,
            storage=None,
            jobs=JobStore(redis),
            settings=temp_collection,
            outbox=None,
        )

    def _document(self, name: str) -> DirectDocumentIn:
        return DirectDocumentIn(
            name=name,
            version="2026",
            fragments=[{"text": "Первый пункт."}, {"text": "Второй пункт."}],
        )

    def test_document_interrupted_mid_flight_is_indexed_exactly_once(
        self, queue_settings, live_queue, live_storage, ingestion
    ):
        doc = self._document(f"ИТЕСТ {uuid.uuid4().hex[:6]}")
        payload_key = f"itest/{uuid.uuid4().hex}.json"
        live_storage.upload(
            payload_key, doc.model_dump_json().encode(), "application/json"
        )
        job_id = str(uuid.uuid4())
        content_hash = DocumentParser.content_hash(
            [{"text": f.text} for f in doc.fragments]
        )
        live_queue.enqueue(
            _entry(
                job_id,
                operation="upload-direct",
                source_object_key=None,
                payload_object_key=payload_key,
                content_hash=content_hash,
                name=doc.name,
            )
        )

        def build_worker(queue: IngestQueue) -> IngestWorker:
            return IngestWorker(
                "itest",
                settings=queue_settings,
                queue=queue,
                jobs=ingestion.jobs,
                parser=DocumentParser(queue_settings),
                ingestion=ingestion,
                document_storage=live_storage,
                user_document_storage=live_storage,
                deps=None,
            )

        # --- process 1: claims the job and dies before committing ---
        claimed = live_queue.claim()
        assert claimed["job_id"] == job_id
        ingestion.ingest_direct(doc, content_hash, doc_id=claimed["doc_id"])
        assert len(ingestion.qdrant.points_by_name(doc.name)) == 2

        # --- process 2: boots, finds the job in flight, resumes it ---
        reborn = IngestQueue(RedisClient(queue_settings), queue_settings)
        assert [e["job_id"] for e in reborn.recover()] == [job_id]
        build_worker(reborn)._process(reborn.claim())

        # Indexed once, not twice: the retry dropped the interrupted attempt's points first.
        points = ingestion.qdrant.points_by_name(doc.name)
        assert len(points) == 2, "a resumed document must not duplicate its fragments"
        assert reborn.pending() == [] and reborn.inflight() == []
        assert json.loads(json.dumps(sorted(p["text"] for p in points))) == [
            "Второй пункт.",
            "Первый пункт.",
        ]
