"""Background workers that drain the durable ingestion queue.

One worker owns one document at a time, start to finish, and ``ingest_concurrency`` of them run
concurrently — which makes the worker count the *only* limit on how many documents touch the
GPU at once (there is no second semaphore inside the service any more). A job that is waiting
is a Redis list entry, not a blocked thread, so a queue of a hundred documents costs the process
nothing.

Each cycle:

1. claim the head job (atomic ``LMOVE`` into the in-flight list);
2. materialize its input — download the original from MinIO and re-parse it, or read the parked
   JSON payload for a direct ingestion;
3. undo whatever a previous attempt of the same job managed to write (see ``_discard_attempt``);
4. run the pipeline in a worker thread;
5. commit — remove the job from the in-flight list — only after the document is indexed.

Step 5 is what survives a restart: a process that dies at step 4 leaves the job in-flight, and
``IngestQueue.recover()`` puts it back at the head of the queue on the next boot.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.dvd_service.dto import DirectDocumentIn
from src.dvd_service.ingest_queue import IngestQueue
from src.dvd_service.routers._upload_common import queued_job
from src.dvd_service.services.dvd_service import IngestionService
from src.dvd_service.services.user_index_service import build_user_ingestion_from_deps

log = structlog.get_logger(__name__)

# Operations whose own semantics already wipe the target before writing ("replace everything
# stored under this name"), so a retry needs no cleanup of its own.
SELF_WIPING = {"reload", "reload-direct"}
DIRECT = {"upload-direct", "reload-direct"}


class IngestWorker:
    """Pulls jobs off :class:`IngestQueue` and runs the ingestion pipeline for each."""

    def __init__(
        self,
        name: str,
        *,
        settings: Settings,
        queue: IngestQueue,
        jobs,
        parser,
        ingestion: IngestionService,
        document_storage: DocumentStorage,
        user_document_storage: DocumentStorage,
        deps,
    ) -> None:
        self.name = name
        self.settings = settings
        self.queue = queue
        self.jobs = jobs
        self.parser = parser
        self.ingestion = ingestion
        self.document_storage = document_storage
        self.user_document_storage = user_document_storage
        self.deps = deps

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name})"

    # --- the loop ---

    async def run(self) -> None:
        """Claim and process jobs until cancelled."""
        log.info("ingest_worker_started", worker=self.name)
        while True:
            try:
                entry = await asyncio.to_thread(self.queue.claim)
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ) as exc:  # noqa: BLE001 — the loop outlives any single failure
                log.warning("ingest_claim_failed", worker=self.name, error=str(exc))
                entry = None
            if entry is None:
                await asyncio.sleep(self.settings.ingest_poll_interval)
                continue
            # Deliberately not shielded from cancellation: on shutdown the job stays on the
            # in-flight list and ``recover()`` requeues it at the next start.
            await asyncio.to_thread(self._process, entry)

    # --- one job ---

    def _process(self, entry: dict) -> None:
        job_id = entry.get("job_id")
        operation = entry.get("operation", "upload")
        self._restore_job_record(entry)
        try:
            self._run_operation(entry)
        except Exception as exc:  # noqa: BLE001 — every failure is a queue decision
            dead, updated = self.queue.fail(entry, str(exc))
            # The service marks its own job "error"; say why it is (or is not) coming back.
            self.jobs.update(
                job_id,
                status="error" if dead else "queued",
                error=(
                    f"{exc} (попыток исчерпано: {updated['attempts']})"
                    if dead
                    else f"{exc} (попытка {updated['attempts']}, будет повторена)"
                ),
            )
            log.warning(
                "ingest_job_failed",
                worker=self.name,
                job_id=job_id,
                operation=operation,
                attempts=updated["attempts"],
                dead_lettered=dead,
                error=str(exc),
            )
            return
        self.queue.commit(entry)
        log.info(
            "ingest_job_done", worker=self.name, job_id=job_id, operation=operation
        )

    def _restore_job_record(self, entry: dict) -> None:
        """Re-create the progress record of a job whose status expired while it waited.

        Job statuses carry a TTL (``DVD_REDIS_JOB_TTL``, a day by default) that is refreshed on
        every write — but a job sitting in a long queue is not being written to. The queue entry
        outlives it, so a document that waited longer than the TTL still gets a row in the admin
        panel instead of silently reappearing as an unnamed job.
        """
        job_id = entry.get("job_id")
        if not job_id or self.jobs.get(job_id):
            return
        scope = entry.get("scope") or {}
        self.jobs.set(
            job_id,
            {
                **queued_job(
                    job_id,
                    entry.get("filename"),
                    entry.get("operation", "upload"),
                    entry.get("name"),
                ),
                "created_at": entry.get("enqueued_at")
                or datetime.now(timezone.utc).isoformat(),
                **{key: scope[key] for key in scope if scope.get(key)},
            },
        )

    def _run_operation(self, entry: dict) -> None:
        operation = entry.get("operation", "upload")
        ingestion = self._ingestion_for(entry)
        self._discard_attempt(entry, ingestion)
        if operation in DIRECT:
            self._run_direct(entry, ingestion)
        else:
            self._run_pipeline(entry, ingestion)

    def _run_pipeline(self, entry: dict, ingestion: IngestionService) -> None:
        """Upload / delta update / full reload of a document from an uploaded file."""
        job_id = entry["job_id"]
        operation = entry["operation"]
        storage = self._storage_for(entry)
        path = self._materialize(entry, storage)
        try:
            raw = self.parser.extract_raw(path)
            meta = dict(entry.get("meta") or {})
            common = dict(
                version_override=entry.get("version"),
                job_id=job_id,
                source_object_key=entry.get("source_object_key"),
                on_identity=lambda name, version: self.queue.set_checkpoint(
                    job_id, name=name, version=version
                ),
                **meta,
            )
            if operation == "upload":
                ingestion.ingest(
                    path,
                    raw,
                    entry["content_hash"],
                    doc_id=entry.get("doc_id"),
                    name_override=entry.get("name"),
                    **common,
                )
            elif operation == "update":
                ingestion.update(
                    entry["name"], path, raw, entry["content_hash"], **common
                )
            elif operation == "reload":
                ingestion.reload(
                    entry["name"], path, raw, entry["content_hash"], **common
                )
            else:
                raise ValueError(f"неизвестная операция очереди: {operation}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _run_direct(self, entry: dict, ingestion: IngestionService) -> None:
        """Direct ingestion — fragments come from the JSON payload parked in MinIO."""
        job_id = entry["job_id"]
        storage = self._storage_for(entry)
        payload_key = entry["payload_object_key"]
        data, _ = storage.download(payload_key)
        doc = DirectDocumentIn.model_validate_json(data)
        if entry["operation"] == "upload-direct":
            ingestion.ingest_direct(
                doc, entry["content_hash"], doc_id=entry.get("doc_id"), job_id=job_id
            )
        else:
            ingestion.reload_direct(doc, entry["content_hash"], job_id=job_id)
        # Only once the document is indexed. A failed job keeps its payload — including a
        # dead-lettered one, which an admin can requeue without re-sending the fragments.
        storage.delete(payload_key)

    # --- helpers ---

    def _ingestion_for(self, entry: dict) -> IngestionService:
        """The shared-corpus service, or a service scoped to the uploading user's index."""
        scope = entry.get("scope") or {}
        if not scope.get("user_id"):
            return self.ingestion
        return build_user_ingestion_from_deps(
            self.deps,
            user_id=scope["user_id"],
            project_id=scope["project_id"],
            scenario_id=scope.get("scenario_id"),
        )

    def _storage_for(self, entry: dict) -> DocumentStorage:
        scope = entry.get("scope") or {}
        return (
            self.user_document_storage
            if scope.get("user_id")
            else self.document_storage
        )

    def _materialize(self, entry: dict, storage: DocumentStorage) -> str:
        """Write the stored original back to the scratch directory for the parser to read."""
        os.makedirs(self.settings.upload_dir, exist_ok=True)
        filename = entry.get("filename") or f"{entry['job_id']}.docx"
        path = os.path.join(
            self.settings.upload_dir, f"{entry['job_id']}_{Path(filename).name}"
        )
        data, _ = storage.download(entry["source_object_key"])
        Path(path).write_bytes(data)
        return path

    def _discard_attempt(self, entry: dict, ingestion: IngestionService) -> None:
        """Undo the partial writes of an earlier attempt so this one starts from a clean slate.

        Node ids are freshly generated on every run, so re-indexing without this would add a
        second copy of every fragment instead of overwriting the first. What has to be undone
        depends on the operation:

        * a fresh upload owns its ``doc_id`` — dropping the points carrying it is exact;
        * a delta update writes under the *base* document's ``doc_id`` and would take previous
          versions with it, so it is undone by version instead (which also strips the new
          version tag from fragments it merely re-tagged);
        * a reload wipes the name before writing anyway.

        Without a checkpoint nothing was written: the pipeline resolves identity before its
        first Qdrant write.
        """
        if not entry.get("attempts"):
            return
        operation = entry.get("operation", "upload")
        if operation in SELF_WIPING:
            return
        checkpoint = self.queue.checkpoint(entry.get("job_id"))
        removed = ingestion.discard_attempt(
            doc_id=entry.get("doc_id") if operation != "update" else None,
            name=checkpoint.get("name") if operation == "update" else None,
            version=checkpoint.get("version") if operation == "update" else None,
        )
        if removed:
            log.info(
                "ingest_retry_cleanup",
                worker=self.name,
                job_id=entry.get("job_id"),
                operation=operation,
                attempts=entry["attempts"],
                **removed,
            )
