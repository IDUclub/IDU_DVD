"""Backfill of the administrative scope for documents that were indexed without one.

Three situations end up here, and one service handles all of them:

* documents ingested before this feature existed — no scope at all;
* documents whose scope could not be resolved at ingest time (Urban API down, an ambiguous
  territory name, an inconclusive head pass);
* documents whose territory a human chose while the Urban API was unreachable — the id is
  stored, the rest of the slice is not.

The work is the same in every case, only the trigger differs: a delayed sweep after startup,
a periodic one, and the admin panel's button. The pending documents are found by their own
payload (``tagging_status="pending"``) rather than by a separate queue — one source of truth,
so the list can never drift from the data it describes.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import structlog
from qdrant_client.models import Filter

from src.api_clients import ChatClient, create_llm
from src.common.config import Settings
from src.common.db.qdrant_client import QdrantRepository, scope_conditions
from src.common.db.redis_client import DocumentRegistry, JobStore
from src.dvd_service.modules.tagging import VersionDetector
from src.dvd_service.modules.territory import (
    SOURCE_MANUAL,
    STATUS_PENDING,
    TerritoryResolver,
)

log = structlog.get_logger(__name__)

# How many leading fragments feed the head pass — the same window the ingest pipeline uses.
_HEAD_FRAGMENTS = 14


class TaggingBackfillService:
    """Resolves the scope of documents left ``pending``, one document at a time."""

    def __init__(
        self,
        qdrant: QdrantRepository,
        registry: DocumentRegistry,
        territory: TerritoryResolver,
        version_detector: VersionDetector,
        jobs: JobStore,
        settings: Settings,
    ) -> None:
        self.qdrant = qdrant
        self.registry = registry
        self.territory = territory
        self.version_detector = version_detector
        self.jobs = jobs
        self.settings = settings
        # One sweep at a time: the LLM and the Urban API are shared with ingestion, and two
        # concurrent sweeps would only fight over the same documents.
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(qdrant={type(self.qdrant).__name__}, "
            f"territory={type(self.territory).__name__})"
        )

    # --- finding work -------------------------------------------------------------------

    def pending_documents(self, limit: int | None = None) -> list[dict]:
        """Pending documents as ``{doc_id, name, payload, texts}``, oldest fragments first.

        Includes user-scoped documents: they carry the same payload fields, so they deserve
        the same tags. Documents that already exhausted their attempt budget are skipped —
        retrying them forever would burn LLM time on a document only a human can fix.
        """
        payloads = self.qdrant.scroll_payloads(
            Filter(must=scope_conditions(tagging_status=STATUS_PENDING))
        )
        documents: dict[str, dict] = {}
        for pl in sorted(payloads, key=lambda p: p.get("order", 0) or 0):
            doc_id = pl.get("doc_id") or ""
            if not doc_id:
                continue
            entry = documents.setdefault(
                doc_id,
                {
                    "doc_id": doc_id,
                    "name": pl.get("name", ""),
                    "payload": pl,
                    "texts": [],
                },
            )
            if len(entry["texts"]) < _HEAD_FRAGMENTS:
                entry["texts"].append(pl.get("text", ""))
        pending = [
            doc
            for doc in documents.values()
            if (doc["payload"].get("tagging_attempts") or 0)
            < self.settings.tagging_max_attempts
        ]
        return pending[:limit] if limit else pending

    # --- resolving one document ---------------------------------------------------------

    def _resolve(self, document: dict, client: ChatClient) -> dict:
        """The scope slice for one pending document (never raises).

        A territory a human already chose is only *completed* here, never reconsidered: the
        stored id is re-resolved into the full slice and stays ``manual``.
        """
        payload = document["payload"]
        chosen = payload.get("territory_id")
        if chosen is not None and payload.get("territory_source") == SOURCE_MANUAL:
            return self.territory.manual_scope(int(chosen))
        parts = [{"text": text} for text in document["texts"] if text]
        head = self.version_detector.detect_head(parts, client)
        return self.territory.from_hints(head)

    def run(
        self,
        *,
        dry_run: bool = False,
        limit: int | None = None,
        job_id: str | None = None,
    ) -> dict:
        """Tag every pending document; returns counts and the per-document outcome.

        Idempotent: a document that resolves is no longer pending and will not be visited
        again, and one that does not resolve keeps its reason and a bumped attempt counter.
        Never raises — a sweep that fails halfway must not take the caller (or the periodic
        timer) down with it.
        """
        if not self._lock.acquire(blocking=False):
            return {
                "status": "already_running",
                "tagged": 0,
                "failed": 0,
                "documents": [],
            }
        started_at = datetime.now(timezone.utc).isoformat()
        tagged = 0
        failed = 0
        outcomes: list[dict] = []
        client = create_llm()
        try:
            documents = self.pending_documents(limit)
            total = len(documents)
            if job_id:
                self.jobs.set(
                    job_id,
                    {
                        "job_id": job_id,
                        "status": "processing",
                        "operation": "tagging-backfill",
                        "stage": "tagging",
                        "stage_index": 1,
                        "stage_total": 1,
                        "progress": 0,
                        "progress_total": total,
                        "task_progress": 0,
                        "overall_progress": 0,
                        "created_at": started_at,
                    },
                )
            for done, document in enumerate(documents, 1):
                scope = self._resolve(document, client)
                resolved = scope.get("territory_id") is not None
                if resolved:
                    tagged += 1
                else:
                    failed += 1
                    scope = {
                        **scope,
                        "tagging_attempts": (
                            document["payload"].get("tagging_attempts") or 0
                        )
                        + 1,
                    }
                if not dry_run:
                    self.qdrant.set_document_payload(document["doc_id"], scope)
                    self._sync_registry(document["doc_id"], scope)
                outcomes.append(
                    {
                        "doc_id": document["doc_id"],
                        "name": document["name"],
                        "territory_id": scope.get("territory_id"),
                        "document_level": scope.get("document_level"),
                        "error": scope.get("tagging_error"),
                    }
                )
                if job_id:
                    percent = int(done / total * 100) if total else 100
                    self.jobs.update(
                        job_id,
                        progress=done,
                        progress_total=total,
                        task_progress=percent,
                        overall_progress=percent,
                    )
            result = {
                "status": "done",
                "dry_run": dry_run,
                "processed": len(documents),
                "tagged": tagged,
                "failed": failed,
                "documents": outcomes,
            }
            if job_id:
                self.jobs.update(job_id, status="done", **{"nodes": tagged})
            log.info(
                "tagging_backfill_done",
                processed=len(documents),
                tagged=tagged,
                failed=failed,
                dry_run=dry_run,
            )
            return result
        except Exception as exc:  # noqa: BLE001 — a sweep must not kill its trigger
            log.exception("tagging_backfill_failed")
            if job_id:
                self.jobs.update(job_id, status="error", error=str(exc))
            return {
                "status": "error",
                "error": str(exc),
                "tagged": tagged,
                "failed": failed,
                "documents": outcomes,
            }
        finally:
            client.close()
            self._lock.release()

    def _sync_registry(self, doc_id: str, scope: dict) -> None:
        """Keep the Redis document summary in step with the payload (best effort)."""
        try:
            record = self.registry.get_document(doc_id)
            if record:
                self.registry.register_document(doc_id, {**record, **scope})
        except Exception as exc:  # noqa: BLE001 — the payload is the source of truth
            log.warning(
                "tagging_backfill_registry_sync_failed", doc_id=doc_id, error=str(exc)
            )
