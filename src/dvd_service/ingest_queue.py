"""Durable FIFO queue of pending ingestion jobs (Redis-backed).

Uploads used to be processed by a FastAPI ``BackgroundTask`` of the request that brought them,
which made a queued document exactly as durable as the process: a restart lost every job that
had not finished, and a waiting job held an anyio worker thread hostage until the GPU freed up.

Here a job is instead *described* — the uploaded original already lives in MinIO, so the entry
only needs the object key plus the parameters of the call — and appended to a Redis list. The
:class:`~src.dvd_service.ingest_worker.IngestWorker` pool drains it, and an entry leaves the
queue only once its document is indexed. Anything interrupted is still in the queue afterwards.

Three lists back it, mirroring :class:`~src.broker.outbox.EventOutbox`:

``{ingest_queue_key}``
    Jobs waiting for a worker. Head is next (``LMOVE`` from the left).
``{ingest_inflight_key}``
    Claimed by a worker and not yet finished. ``recover()`` returns them to the queue at
    startup — a non-empty in-flight list means the previous process died mid-document.
``{ingest_dead_key}``
    Jobs that exhausted ``ingest_max_attempts``. Never retried automatically; an admin
    inspects them (``GET /documents/jobs/dead``) and requeues explicitly.

Entries are serialized with sorted keys so that the exact string pushed onto the in-flight list
can be found again by value (``LREM``) without carrying the raw JSON around.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from src.common.config import Settings
from src.common.db.redis_client import RedisClient

log = structlog.get_logger(__name__)


def _dump(entry: dict) -> str:
    """Serialize an entry canonically — re-dumping a parsed entry must reproduce the string."""
    return json.dumps(entry, ensure_ascii=False, sort_keys=True)


class IngestQueue:
    """Pending ingestion jobs. Keys: pending / in-flight / dead lists + identity checkpoints."""

    def __init__(self, client: RedisClient, settings: Settings) -> None:
        self.r = client.r
        self.key = settings.ingest_queue_key
        self.inflight_key = settings.ingest_inflight_key
        self.dead_key = settings.ingest_dead_key
        self.checkpoint_prefix = settings.ingest_checkpoint_prefix
        self.max_attempts = max(1, settings.ingest_max_attempts)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(key={self.key}, inflight={self.inflight_key}, "
            f"dead={self.dead_key}, max_attempts={self.max_attempts})"
        )

    # --- producing ---

    def enqueue(self, entry: dict) -> dict:
        """Append a job to the tail of the queue (called from the upload endpoints)."""
        entry = {
            **entry,
            "attempts": int(entry.get("attempts", 0)),
            "enqueued_at": entry.get("enqueued_at")
            or datetime.now(timezone.utc).isoformat(),
        }
        self.r.rpush(self.key, _dump(entry))
        return entry

    # --- consuming ---

    def claim(self) -> dict | None:
        """Move the head job to the in-flight list and return it (atomic), or ``None`` if idle.

        Atomicity is what allows more than one worker: two workers can never take the same
        job, because ``LMOVE`` pops and pushes in a single Redis operation.
        """
        raw = self.r.lmove(self.key, self.inflight_key, "LEFT", "RIGHT")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Unparseable entry: drop it rather than block the queue forever.
            self.r.lrem(self.inflight_key, 1, raw)
            log.warning("ingest_queue_entry_corrupt", raw=str(raw)[:500])
            return None

    def commit(self, entry: dict) -> None:
        """Drop a finished job from the in-flight list."""
        self.r.lrem(self.inflight_key, 1, _dump(entry))
        self.drop_checkpoint(entry.get("job_id"))

    def fail(self, entry: dict, error: str) -> tuple[bool, dict]:
        """Count a failed attempt: requeue the job, or dead-letter it once attempts run out.

        Returns ``(dead, entry)`` where ``entry`` is the updated job. A requeued job goes back
        to the *head* of the queue: it was there first, and letting a retry drift behind a
        freshly uploaded batch is how documents get forgotten.
        """
        updated = {
            **entry,
            "attempts": int(entry.get("attempts", 0)) + 1,
            "last_error": error[:1000],
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        dead = updated["attempts"] >= self.max_attempts
        pipe = self.r.pipeline()
        pipe.lrem(self.inflight_key, 1, _dump(entry))
        if dead:
            pipe.rpush(self.dead_key, _dump(updated))
        else:
            pipe.lpush(self.key, _dump(updated))
        pipe.execute()
        if dead:
            self.drop_checkpoint(updated.get("job_id"))
        return dead, updated

    def recover(self) -> list[dict]:
        """Return every in-flight job to the head of the queue (called once at startup).

        A job on the in-flight list at startup belongs to a process that is gone: it was
        claimed but never committed. Order is preserved and the jobs go back in front of
        anything that arrived later.

        Interruption counts as a failed attempt, so a document that kills the process takes
        itself out of the queue after ``ingest_max_attempts`` boots instead of crash-looping
        the service forever. Such jobs are dead-lettered here rather than requeued.

        Assumes a single app instance per Redis prefix (as deployed) — with siblings this
        would steal jobs another instance is actively processing.
        """
        raws = self.r.lrange(self.inflight_key, 0, -1)
        if not raws:
            return []
        requeued: list[dict] = []
        dead: list[dict] = []
        for raw in raws:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entry = {
                **entry,
                "attempts": int(entry.get("attempts", 0)) + 1,
                "last_error": "Обработка прервана перезапуском сервиса",
            }
            (dead if entry["attempts"] >= self.max_attempts else requeued).append(entry)
        pipe = self.r.pipeline()
        for entry in reversed(requeued):
            pipe.lpush(self.key, _dump(entry))
        for entry in dead:
            pipe.rpush(self.dead_key, _dump(entry))
        pipe.delete(self.inflight_key)
        pipe.execute()
        for entry in dead:
            self.drop_checkpoint(entry.get("job_id"))
        return requeued

    # --- identity checkpoint (retry cleanup) ---

    def _checkpoint_key(self, job_id: str) -> str:
        return f"{self.checkpoint_prefix}:{job_id}"

    def set_checkpoint(self, job_id: str, **fields) -> None:
        """Record what an attempt resolved (name/version) before it started writing to Qdrant.

        A retry needs this to undo a partial write: fragments of an interrupted delta update
        are only identifiable by the version they were tagged with.
        """
        if not job_id:
            return
        self.r.set(self._checkpoint_key(job_id), _dump(fields))

    def checkpoint(self, job_id: str | None) -> dict:
        if not job_id:
            return {}
        raw = self.r.get(self._checkpoint_key(job_id))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def drop_checkpoint(self, job_id: str | None) -> None:
        if job_id:
            self.r.delete(self._checkpoint_key(job_id))

    # --- inspecting / operating ---

    def size(self) -> int:
        return self.r.llen(self.key)

    def inflight_size(self) -> int:
        return self.r.llen(self.inflight_key)

    def pending(self) -> list[dict]:
        return self._read(self.key)

    def inflight(self) -> list[dict]:
        return self._read(self.inflight_key)

    def dead(self) -> list[dict]:
        return self._read(self.dead_key)

    def _read(self, key: str) -> list[dict]:
        entries = []
        for raw in self.r.lrange(key, 0, -1):
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return entries

    def requeue_dead(self, job_id: str) -> dict | None:
        """Move one dead-lettered job back to the queue with its attempt counter reset."""
        for raw in self.r.lrange(self.dead_key, 0, -1):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("job_id") != job_id:
                continue
            revived = {
                **{
                    k: v
                    for k, v in entry.items()
                    if k not in {"last_error", "failed_at"}
                },
                "attempts": 0,
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
            }
            pipe = self.r.pipeline()
            pipe.lrem(self.dead_key, 1, raw)
            pipe.rpush(self.key, _dump(revived))
            pipe.execute()
            return revived
        return None
