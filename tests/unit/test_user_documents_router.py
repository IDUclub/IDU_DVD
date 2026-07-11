"""Unit tests for src/dvd_service/routers/user_documents — user document index endpoints.

Builds a FastAPI app from the router, overriding per-request dependencies with fakes/real-but-
fakeredis-backed objects, and monkeypatches ``build_user_ingestion_from_deps`` so uploads/updates
exercise the router's own logic (job queueing, index auto-creation, 404s) without running the real
ingestion pipeline (that pipeline — reused unmodified from ``/documents`` — is covered by
``test_services.py``/``test_routers.py``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.common.config import Settings
from src.common.db.redis_client import RedisClient, UserIndexRegistry
from src.dependencies import Dependencies
from src.dvd_service.routers import user_documents_router
from src.dvd_service.services.user_index_service import UserIndexService


class FakeParser:
    def extract_raw(self, path):
        return [{"text": "x", "category": "NarrativeText", "html": None}]

    def content_hash(self, raw):
        return "hash-1"


class FakeJobs:
    def __init__(self):
        self.store = {}

    def set(self, jid, data):
        self.store[jid] = data

    def get(self, jid):
        return self.store.get(jid)


class FakeIngestion:
    def __init__(self):
        self.ingest_calls = []
        self.update_calls = []
        self.reload_calls = []
        self.delete_calls = []

    def ingest(self, *a, **k):
        self.ingest_calls.append((a, k))
        return {}

    def update(self, *a, **k):
        self.update_calls.append((a, k))
        return {}

    def reload(self, *a, **k):
        self.reload_calls.append((a, k))
        return {}

    def delete_document(self, name, version=None):
        self.delete_calls.append((name, version))
        if name == "нет такого":
            raise KeyError(f"документ не найден: {name}")
        return {
            "name": name,
            "versions_removed": [version] if version else ["v1"],
            "points_deleted": 1,
            "points_updated": 0,
        }


@pytest.fixture(autouse=True)
def _reset_singleton():
    Dependencies.reset()
    yield
    Dependencies.reset()


@pytest.fixture
def client(tmp_path, settings, fake_redis, fake_qdrant, fake_document_storage, monkeypatch):
    redis_client = RedisClient(settings)
    index_registry = UserIndexRegistry(redis_client, prefix=settings.registry_prefix)
    user_index_service = UserIndexService(
        fake_qdrant, redis_client, index_registry, settings, storage=fake_document_storage
    )
    fake_ingestion = FakeIngestion()

    # _build_ingestion() reaches Dependencies.instance() directly — populate the singleton so
    # that call doesn't raise; the actual field values don't matter since
    # build_user_ingestion_from_deps is monkeypatched below to ignore them.
    fields = {n: object() for n in Dependencies._FIELDS}
    Dependencies().set(**fields)

    import src.dvd_service.routers.user_documents as router_mod

    monkeypatch.setattr(
        router_mod, "build_user_ingestion_from_deps", lambda *a, **k: fake_ingestion
    )

    upload_settings = Settings(upload_dir=str(tmp_path))
    app = FastAPI()
    app.include_router(user_documents_router)
    app.dependency_overrides[Dependencies.get_settings] = lambda: upload_settings
    app.dependency_overrides[Dependencies.get_parser] = lambda: FakeParser()
    app.dependency_overrides[Dependencies.get_redis] = lambda: redis_client
    app.dependency_overrides[Dependencies.get_user_index_registry] = lambda: index_registry
    app.dependency_overrides[Dependencies.get_user_index_service] = lambda: user_index_service
    app.dependency_overrides[Dependencies.get_qdrant] = lambda: fake_qdrant
    app.dependency_overrides[Dependencies.get_user_document_storage] = (
        lambda: fake_document_storage
    )
    app.dependency_overrides[Dependencies.get_jobs] = lambda: FakeJobs()
    with TestClient(app) as c:
        yield c, fake_ingestion, index_registry, fake_document_storage


class TestIndexLifecycle:
    def test_create_index(self, client):
        c, _, _, _ = client
        resp = c.post(
            "/user-documents/index",
            json={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["user_id"] == "u1" and body["scenario_id"] == "s1"
        assert body["document_count"] == 0

    def test_create_duplicate_index_returns_409(self, client):
        c, _, _, _ = client
        payload = {"user_id": "u1", "scenario_id": "s1", "project_id": "p1"}
        c.post("/user-documents/index", json=payload)
        resp = c.post("/user-documents/index", json=payload)
        assert resp.status_code == 409

    def test_list_indices(self, client):
        c, _, _, _ = client
        c.post(
            "/user-documents/index",
            json={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        resp = c.get("/user-documents/index", params={"user_id": "u1"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_delete_index(self, client):
        c, _, index_registry, _ = client
        c.post(
            "/user-documents/index",
            json={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        resp = c.delete(
            "/user-documents/index", params={"user_id": "u1", "scenario_id": "s1"}
        )
        assert resp.status_code == 200
        assert index_registry.get("u1", "s1") is None

    def test_delete_missing_index_returns_404(self, client):
        c, _, _, _ = client
        resp = c.delete(
            "/user-documents/index", params={"user_id": "u1", "scenario_id": "ghost"}
        )
        assert resp.status_code == 404


class TestUploadDocument:
    def test_auto_creates_index_and_queues_ingest(self, client):
        c, fake_ingestion, index_registry, _ = client
        resp = c.post(
            "/user-documents",
            files={"file": ("doc.docx", b"data")},
            data={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"
        assert index_registry.get("u1", "s1") is not None
        assert fake_ingestion.ingest_calls

    def test_honors_parent_scenario_id_on_first_upload(self, client):
        c, _, index_registry, _ = client
        c.post(
            "/user-documents",
            files={"file": ("doc.docx", b"data")},
            data={
                "user_id": "u1",
                "scenario_id": "s2",
                "project_id": "p1",
                "parent_scenario_id": "s1",
            },
        )
        assert index_registry.get("u1", "s2")["parent_scenario_id"] == "s1"

    def test_unsupported_extension_rejected(self, client):
        c, _, _, _ = client
        resp = c.post(
            "/user-documents",
            files={"file": ("scan.pdf", b"data")},
            data={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        assert resp.status_code == 415

    def test_saves_source_to_minio_and_forwards_object_key(self, client):
        c, fake_ingestion, _, storage = client
        resp = c.post(
            "/user-documents",
            files={"file": ("doc.docx", b"data")},
            data={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        assert resp.status_code == 202
        assert storage.upload_calls
        _, kwargs = fake_ingestion.ingest_calls[-1]
        assert kwargs["source_object_key"] == storage.upload_calls[-1]

    def test_storage_failure_rejects_upload_and_never_queues_a_job(self, client):
        c, fake_ingestion, _, storage = client
        storage.fail_upload = True
        resp = c.post(
            "/user-documents",
            files={"file": ("doc.docx", b"data")},
            data={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )
        assert resp.status_code == 502
        assert not fake_ingestion.ingest_calls


class TestUpdateReloadDeleteDocument:
    def _create_index(self, c):
        c.post(
            "/user-documents/index",
            json={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
        )

    def test_update_unknown_index_returns_404(self, client):
        c, _, _, _ = client
        resp = c.patch(
            "/user-documents/doc",
            files={"file": ("doc.docx", b"data")},
            params={"user_id": "u1", "scenario_id": "ghost"},
        )
        assert resp.status_code == 404

    def test_update_unknown_document_returns_404(self, client):
        c, _, _, _ = client
        self._create_index(c)
        resp = c.patch(
            "/user-documents/unknown-doc",
            files={"file": ("doc.docx", b"data")},
            params={"user_id": "u1", "scenario_id": "s1"},
        )
        assert resp.status_code == 404

    def test_reload_unknown_index_returns_404(self, client):
        c, _, _, _ = client
        resp = c.put(
            "/user-documents/doc",
            files={"file": ("doc.docx", b"data")},
            params={"user_id": "u1", "scenario_id": "ghost"},
        )
        assert resp.status_code == 404

    def test_reload_queues_background_reload(self, client):
        c, fake_ingestion, _, _ = client
        self._create_index(c)
        resp = c.put(
            "/user-documents/doc",
            files={"file": ("doc.docx", b"data")},
            params={"user_id": "u1", "scenario_id": "s1"},
        )
        assert resp.status_code == 202
        assert fake_ingestion.reload_calls

    def test_delete_unknown_index_returns_404(self, client):
        c, _, _, _ = client
        resp = c.delete(
            "/user-documents/doc", params={"user_id": "u1", "scenario_id": "ghost"}
        )
        assert resp.status_code == 404

    def test_delete_document_success(self, client):
        c, fake_ingestion, _, _ = client
        self._create_index(c)
        resp = c.delete(
            "/user-documents/doc", params={"user_id": "u1", "scenario_id": "s1"}
        )
        assert resp.status_code == 200
        assert fake_ingestion.delete_calls == [("doc", None)]

    def test_delete_unknown_document_returns_404(self, client):
        c, _, _, _ = client
        self._create_index(c)
        resp = c.delete(
            "/user-documents/нет такого", params={"user_id": "u1", "scenario_id": "s1"}
        )
        assert resp.status_code == 404


class TestListUserDocuments:
    def test_empty_index_returns_no_documents(self, client):
        c, _, _, _ = client
        resp = c.get(
            "/user-documents", params={"user_id": "u1", "scenario_id": "s1"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "documents": []}


class TestDownloadUserSource:
    @staticmethod
    def _create_index(c, user_id, scenario_id, parent_scenario_id=None):
        c.post(
            "/user-documents/index",
            json={
                "user_id": user_id,
                "scenario_id": scenario_id,
                "project_id": "p1",
                "parent_scenario_id": parent_scenario_id,
            },
        )

    @staticmethod
    def _seed_point(fake_qdrant, storage, *, user_id, scenario_id, key, data):
        from qdrant_client.models import PointStruct

        fake_qdrant.upsert(
            [
                PointStruct(
                    id=key,
                    vector=[0.0],
                    payload={
                        "name": "doc",
                        "version": "v1",
                        "user_id": user_id,
                        "scenario_id": scenario_id,
                        "source_object_key": key,
                    },
                )
            ]
        )
        storage.upload(key, data, "application/octet-stream")

    def test_downloads_the_original_file(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._seed_point(fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi")

        resp = c.get(
            "/user-documents/doc/source", params={"user_id": "u1", "scenario_id": "s1"}
        )
        assert resp.status_code == 200
        assert resp.content == b"hi"

    def test_different_user_gets_404(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._seed_point(fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi")

        resp = c.get(
            "/user-documents/doc/source", params={"user_id": "u2", "scenario_id": "s1"}
        )
        assert resp.status_code == 404

    def test_inherited_document_downloads_via_child_scenario(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._create_index(c, "u1", "s2", parent_scenario_id="s1")
        self._seed_point(fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi")

        resp = c.get(
            "/user-documents/doc/source", params={"user_id": "u1", "scenario_id": "s2"}
        )
        assert resp.status_code == 200
        assert resp.content == b"hi"

    def test_include_inherited_false_excludes_parent_scenario(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._create_index(c, "u1", "s2", parent_scenario_id="s1")
        self._seed_point(fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi")

        resp = c.get(
            "/user-documents/doc/source",
            params={"user_id": "u1", "scenario_id": "s2", "include_inherited": False},
        )
        assert resp.status_code == 404
