"""Unit tests for direct document ingestion — caller-supplied fragments straight into Qdrant.

Two layers:
  * the HTTP router (``/documents/direct``) with faked dependencies — batching, per-document
    duplicate rejection, structural validation;
  * ``IngestionService.ingest_direct`` / ``reload_direct`` wired with the real registry
    (fakeredis) + Qdrant/embedder fakes — payload shape, neighbour links, registry state, events.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.dvd_service.services.dvd_service as svc
from src.broker.outbox import EventOutbox
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
from src.dependencies import Dependencies
from src.dvd_service.dto import DirectDocumentIn, DirectFragmentIn
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.routers import direct_documents_router
from src.dvd_service.services.dvd_service import IngestionService


def _hash(doc: DirectDocumentIn) -> str:
    return DocumentParser.content_hash([{"text": f.text} for f in doc.fragments])


# --------------------------------------------------------------------------------------
# Router layer — faked dependencies
# --------------------------------------------------------------------------------------
class FakeRegistry:
    def __init__(self):
        self.dup = False

    def has_hash(self, h):
        return self.dup

    def hash_info(self, h):
        return {"name": "N", "version": "V", "doc_id": "d1"}

    def remove_hashes(self, name, version=None):
        return 1

    def unregister_document(self, doc_id):
        pass

    def unregister_name(self, name):
        pass


class FakeJobs:
    def __init__(self):
        self.store = {}

    def set(self, jid, data):
        self.store[jid] = data

    def get(self, jid):
        return self.store.get(jid)


class FakeQdrant:
    def __init__(self):
        self.by_name = {}

    def points_by_name(self, name, extra_must=None):
        return self.by_name.get(name, [])


class FakeIngestion:
    def __init__(self, qdrant):
        self.qdrant = qdrant
        self.ingest_calls = []
        self.reload_calls = []

    def ingest_direct(self, doc, content_hash, *, job_id=None, emit_event=True):
        self.ingest_calls.append((doc.name, content_hash, job_id))
        return {}

    def reload_direct(self, doc, content_hash, *, job_id=None):
        self.reload_calls.append((doc.name, content_hash, job_id))
        return {}


@pytest.fixture
def client():
    fakes = {
        "registry": FakeRegistry(),
        "jobs": FakeJobs(),
        "qdrant": FakeQdrant(),
    }
    fakes["ingestion"] = FakeIngestion(fakes["qdrant"])
    app = FastAPI()
    app.include_router(direct_documents_router)
    app.dependency_overrides[Dependencies.get_registry] = lambda: fakes["registry"]
    app.dependency_overrides[Dependencies.get_jobs] = lambda: fakes["jobs"]
    app.dependency_overrides[Dependencies.get_ingestion] = lambda: fakes["ingestion"]
    with TestClient(app) as c:
        yield c, fakes


def _body(name: str, *texts: str) -> dict:
    return {"name": name, "fragments": [{"text": t} for t in texts]}


class TestUploadDirectRouter:
    def test_single_document_queues_a_job(self, client):
        c, fakes = client
        resp = c.post("/documents/direct", json=[_body("ДОК 1", "а", "б")])
        assert resp.status_code == 202
        body = resp.json()
        assert len(body) == 1
        assert body[0]["status"] == "queued"
        assert body[0]["name"] == "ДОК 1"
        assert body[0]["job_id"] in fakes["jobs"].store
        assert fakes["ingestion"].ingest_calls, "background ingest must run"

    def test_batch_queues_one_job_per_document(self, client):
        c, fakes = client
        resp = c.post(
            "/documents/direct",
            json=[_body("A", "x"), _body("B", "y"), _body("C", "z")],
        )
        assert resp.status_code == 202
        body = resp.json()
        assert [r["status"] for r in body] == ["queued", "queued", "queued"]
        assert len({r["job_id"] for r in body}) == 3
        assert len(fakes["ingestion"].ingest_calls) == 3

    def test_duplicate_rejected_per_document(self, client):
        c, fakes = client
        fakes["registry"].dup = True
        # A live duplicate: the registered document is really present in Qdrant.
        fakes["qdrant"].by_name["N"] = [{"id": "p1", "name": "N"}]
        resp = c.post("/documents/direct", json=[_body("ДОК 1", "а")])
        assert resp.status_code == 202
        body = resp.json()
        assert body[0]["status"] == "rejected"
        assert body[0]["job_id"] is None
        assert body[0]["error"]
        assert not fakes["ingestion"].ingest_calls, "a duplicate must not be queued"

    def test_ghost_duplicate_is_dropped_and_document_proceeds(self, client):
        c, fakes = client
        fakes["registry"].dup = True  # …but Qdrant holds nothing under that name
        resp = c.post("/documents/direct", json=[_body("ДОК 1", "а")])
        assert resp.status_code == 202
        assert resp.json()[0]["status"] == "queued"
        assert fakes["ingestion"].ingest_calls

    def test_missing_name_is_422(self, client):
        c, _ = client
        resp = c.post("/documents/direct", json=[{"fragments": [{"text": "а"}]}])
        assert resp.status_code == 422

    def test_empty_fragments_is_422(self, client):
        c, _ = client
        resp = c.post("/documents/direct", json=[{"name": "ДОК", "fragments": []}])
        assert resp.status_code == 422

    def test_blank_fragment_text_is_422(self, client):
        c, _ = client
        resp = c.post(
            "/documents/direct", json=[{"name": "ДОК", "fragments": [{"text": "   "}]}]
        )
        assert resp.status_code == 422


class TestReplaceDirectRouter:
    def test_put_queues_reload_without_dedup(self, client):
        c, fakes = client
        fakes["registry"].dup = True  # PUT never rejects duplicates
        resp = c.put("/documents/direct", json=[_body("ДОК 1", "а")])
        assert resp.status_code == 202
        assert resp.json()[0]["status"] == "queued"
        assert fakes["ingestion"].reload_calls


# --------------------------------------------------------------------------------------
# Service layer — real registry (fakeredis) + Qdrant/embedder fakes
# --------------------------------------------------------------------------------------
@pytest.fixture
def ingestion(settings, fake_ollama, fake_qdrant, fake_redis, monkeypatch):
    monkeypatch.setattr(svc, "create_embedder", lambda *a, **k: fake_ollama)
    redis_client = RedisClient(settings)
    jobs = JobStore(redis_client)
    registry = DocumentRegistry(redis_client)
    outbox = EventOutbox(redis_client, settings)
    service = IngestionService(
        parser=None,
        structure=None,
        hierarchy=None,
        version_detector=None,
        reference_extractor=None,
        reference_resolver=None,
        qdrant=fake_qdrant,
        registry=registry,
        storage=None,
        jobs=jobs,
        settings=settings,
        outbox=outbox,
    )
    return service, fake_qdrant, registry, jobs, outbox


def _doc(name="ДОК 1", texts=("первый фрагмент", "второй фрагмент"), **kw):
    return DirectDocumentIn(
        name=name, fragments=[DirectFragmentIn(text=t) for t in texts], **kw
    )


class TestIngestDirectService:
    def test_indexes_fragments_and_registers_document(self, ingestion):
        service, qdrant, registry, jobs, _ = ingestion
        doc = _doc()
        res = service.ingest_direct(doc, _hash(doc), job_id="j1")

        assert res["nodes"] == 2
        assert res["name"] == "ДОК 1"
        assert res["version"] == "1"  # no 4-digit group in the name
        assert jobs.get("j1")["status"] == "done"
        assert registry.has_hash(_hash(doc))
        assert res["version"] in registry.versions("ДОК 1")
        assert registry.get_document(res["doc_id"])["node_count"] == 2

    def test_neighbour_links_and_order_follow_array_position(self, ingestion):
        service, qdrant, _, _, _ = ingestion
        doc = _doc()
        service.ingest_direct(doc, _hash(doc))

        points = sorted(qdrant.points_by_name("ДОК 1"), key=lambda p: p["order"])
        assert [p["order"] for p in points] == [0, 1]
        assert points[0]["prev_id"] is None
        assert points[0]["next_id"] == points[1]["id"]
        assert points[1]["prev_id"] == points[0]["id"]
        assert points[1]["next_id"] is None
        assert points[0]["parser_version"] is None  # no parser ran

    def test_version_from_trailing_digits(self, ingestion):
        service, _, registry, _, _ = ingestion
        doc = _doc(name="СП 2.13130.2020")
        res = service.ingest_direct(doc, _hash(doc))
        assert res["version"] == "2020"

    def test_fragment_metadata_layers_over_document_metadata(self, ingestion):
        service, qdrant, _, _, _ = ingestion
        doc = DirectDocumentIn(
            name="ДОК",
            metadata={"src": "doc", "shared": 1},
            fragments=[DirectFragmentIn(text="t", metadata={"shared": 2, "own": "x"})],
        )
        service.ingest_direct(doc, _hash(doc))
        pl = qdrant.points_by_name("ДОК")[0]
        assert pl["metadata"] == {"src": "doc", "shared": 2, "own": "x"}

    def test_emits_direct_processed_event(self, ingestion):
        service, _, _, _, outbox = ingestion
        doc = _doc()
        service.ingest_direct(doc, _hash(doc))
        assert outbox.peek()["model"] == "DirectDocumentProcessed"

    def test_unknown_provider_rejected(self, ingestion):
        service, _, _, _, _ = ingestion
        doc = DirectDocumentIn(
            name="ДОК 1",
            embedding_provider="no-such-provider",
            fragments=[DirectFragmentIn(text="t")],
        )
        with pytest.raises(ValueError, match="недоступен"):
            service.ingest_direct(doc, _hash(doc))


class TestReloadDirectService:
    def test_replaces_existing_and_emits_updated(self, ingestion):
        service, qdrant, registry, _, outbox = ingestion
        first = _doc(texts=("a", "b", "c"))
        service.ingest_direct(first, _hash(first))
        assert len(qdrant.points_by_name("ДОК 1")) == 3
        outbox.commit()  # drop the DirectDocumentProcessed from the initial ingest

        second = _doc(texts=("only one",))
        res = service.reload_direct(second, _hash(second))
        points = qdrant.points_by_name("ДОК 1")
        assert len(points) == 1  # every prior fragment wiped
        assert res["nodes"] == 1
        assert outbox.peek()["model"] == "DirectDocumentUpdated"

    def test_reload_of_absent_document_emits_processed(self, ingestion):
        service, _, _, _, outbox = ingestion
        doc = _doc()
        service.reload_direct(doc, _hash(doc))
        assert outbox.peek()["model"] == "DirectDocumentProcessed"
