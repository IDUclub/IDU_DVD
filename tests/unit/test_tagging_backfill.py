"""Unit tests for src/dvd_service/services/tagging_backfill — TaggingBackfillService.

Covers what the sweep must guarantee: it finds documents by their own payload, it completes a
manually chosen territory without reconsidering it, it is idempotent, it respects the attempt
cap, dry runs write nothing, and a failure inside a sweep never propagates to its trigger.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client.models import PointStruct
from unit.test_territory import FakeUrbanApi

import src.dvd_service.services.tagging_backfill as backfill_module
from src.api_clients import COUNTRY_TERRITORY_ID
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
from src.dvd_service.modules.tagging import VersionDetector
from src.dvd_service.modules.territory import (
    SOURCE_MANUAL,
    TerritoryResolver,
    untagged_scope,
)
from src.dvd_service.services.tagging_backfill import TaggingBackfillService


@pytest.fixture
def backfill(settings, fake_qdrant, fake_redis, fake_ollama, monkeypatch):
    """The service over faked boundaries, with the head pass answering "federal"."""
    monkeypatch.setattr(backfill_module, "OllamaClient", lambda *a, **k: fake_ollama)
    redis_client = RedisClient(settings)
    return TaggingBackfillService(
        fake_qdrant,
        DocumentRegistry(redis_client),
        TerritoryResolver(FakeUrbanApi()),
        VersionDetector(),
        JobStore(redis_client),
        settings,
    )


def _store_pending(qdrant, name="ПЗЗ", territory_id=None, fragments=3, **scope):
    """A document stored the way an ingest during an Urban API outage leaves it.

    Written straight into the (faked) collection rather than through the pipeline: the sweep's
    contract is with the stored payload, which is also the only thing it can see in production
    for documents indexed long before this feature existed.
    """
    doc_id = f"doc-{name}"
    payload = {
        **untagged_scope(),
        "doc_id": doc_id,
        "name": name,
        "version": "1",
        "territory_id": territory_id,
        "territory_source": SOURCE_MANUAL if territory_id else "unset",
        "tagging_error": "Urban API недоступен: connection refused",
        **scope,
    }
    qdrant.upsert(
        [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=[0.1, 0.2],
                payload={
                    **payload,
                    "order": order,
                    "text": f"{name} — фрагмент {order}",
                },
            )
            for order in range(fragments)
        ]
    )
    return {"doc_id": doc_id}


class TestFindingWork:
    def test_pending_documents_are_found_by_their_payload(self, backfill):
        _store_pending(backfill.qdrant)
        pending = backfill.pending_documents()
        assert [doc["name"] for doc in pending] == ["ПЗЗ"]
        assert pending[0]["texts"], "the head fragments feed the retry's LLM pass"

    def test_tagged_documents_are_not_pending(self, backfill):
        _store_pending(
            backfill.qdrant, name="готов", tagging_status="ok", territory_id=1
        )
        assert backfill.pending_documents() == []

    def test_documents_over_the_attempt_cap_are_skipped(self, backfill, settings):
        result = _store_pending(backfill.qdrant)
        backfill.qdrant.set_document_payload(
            result["doc_id"], {"tagging_attempts": settings.tagging_max_attempts}
        )
        assert backfill.pending_documents() == []


class TestSweep:
    def test_a_pending_document_gets_tagged(self, backfill):
        result = _store_pending(backfill.qdrant)
        outcome = backfill.run()
        assert outcome["tagged"] == 1 and outcome["failed"] == 0
        payload = backfill.qdrant.list_by_doc(result["doc_id"])[0]
        # the faked head pass reports a federal document
        assert payload["territory_id"] == COUNTRY_TERRITORY_ID
        assert payload["tagging_status"] == "ok"
        assert payload["territory_source"] == "auto"

    def test_a_manual_choice_is_completed_not_reconsidered(self, backfill):
        """The admin picked Vyborg while the Urban API was down; the sweep finishes the job."""
        result = _store_pending(backfill.qdrant, territory_id=54)
        backfill.run()
        payload = backfill.qdrant.list_by_doc(result["doc_id"])[0]
        assert (
            payload["territory_id"] == 54
        )  # not the "federal" the head pass would say
        assert payload["territory_source"] == "manual"
        assert payload["document_level"] == "municipal"
        assert payload["tagging_status"] == "ok"

    def test_running_twice_changes_nothing_the_second_time(self, backfill):
        _store_pending(backfill.qdrant)
        assert backfill.run()["tagged"] == 1
        assert backfill.run()["processed"] == 0

    def test_dry_run_reports_without_writing(self, backfill):
        result = _store_pending(backfill.qdrant)
        outcome = backfill.run(dry_run=True)
        assert outcome["tagged"] == 1
        payload = backfill.qdrant.list_by_doc(result["doc_id"])[0]
        assert payload["tagging_status"] == "pending"

    def test_limit_caps_the_sweep(self, backfill):
        _store_pending(backfill.qdrant, name="ПЗЗ 1")
        _store_pending(backfill.qdrant, name="ПЗЗ 2")
        assert backfill.run(limit=1)["processed"] == 1

    def test_an_unresolvable_document_records_the_reason_and_bumps_attempts(
        self, settings, fake_qdrant, fake_redis, fake_ollama, monkeypatch
    ):
        """Urban API still down: the document stays pending, but visibly so."""
        monkeypatch.setattr(
            backfill_module, "OllamaClient", lambda *a, **k: fake_ollama
        )
        redis_client = RedisClient(settings)
        service = TaggingBackfillService(
            fake_qdrant,
            DocumentRegistry(redis_client),
            TerritoryResolver(FakeUrbanApi(broken=True)),
            VersionDetector(),
            JobStore(redis_client),
            settings,
        )
        result = _store_pending(fake_qdrant)
        outcome = service.run()
        assert outcome["failed"] == 1 and outcome["tagged"] == 0
        payload = service.qdrant.list_by_doc(result["doc_id"])[0]
        assert payload["tagging_status"] == "pending"
        assert payload["tagging_attempts"] == 1
        assert "Urban API" in payload["tagging_error"]

    def test_progress_is_reported_through_the_job_store(self, backfill):
        _store_pending(backfill.qdrant)
        backfill.run(job_id="jb")
        job = backfill.jobs.get("jb")
        assert job["status"] == "done" and job["operation"] == "tagging-backfill"

    def test_a_failure_is_reported_not_raised(self, backfill):
        """The periodic timer must survive a broken sweep."""
        _store_pending(backfill.qdrant)

        def boom(*_args, **_kwargs):
            raise RuntimeError("qdrant is gone")

        backfill.qdrant.set_document_payload = boom
        outcome = backfill.run()
        assert outcome["status"] == "error" and "qdrant is gone" in outcome["error"]

    def test_a_second_concurrent_sweep_is_refused(self, backfill):
        backfill._lock.acquire()
        try:
            assert backfill.run()["status"] == "already_running"
        finally:
            backfill._lock.release()
