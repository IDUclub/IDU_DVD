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
from fastmcp.server.auth import AccessToken

from src.api_clients import TerritoryNotFound, UrbanApiError
from src.common.auth import keycloak_token_verifier
from src.common.config import Settings
from src.common.db.redis_client import DocumentRegistry, RedisClient, UserIndexRegistry
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


class FakeUrbanApi:
    def project_id_for_scenario(self, _scenario_id, user_id):
        assert user_id
        return "p1"


class FakeTerritory:
    def filter_ids(self, territory_ids):
        return sorted({1, *(int(value) for value in territory_ids)})

    def by_territory_id(self, territory_id):
        territory_id = int(territory_id)
        return {
            "document_level": "municipal",
            "territory_id": territory_id,
            "territory_name": "Выборгский муниципальный район",
            "territory_type_id": 2,
            "territory_type_name": "Муниципальное образование",
            "territory_path": [12639, 1, territory_id],
            "territory_source": "manual",
            "tagging_status": "ok",
            "tagging_error": None,
        }


class FakeScopedQdrant:
    """Minimal stand-in for the user-scoped repository: only dedup touches it here."""

    def __init__(self, names: set[str] | None = None) -> None:
        self.names = names or set()

    def points_by_name(self, name):
        return [{"name": name, "id": "p1"}] if name in self.names else []


class FakeIngestion:
    def __init__(self, qdrant=None):
        # Dedup consults it to tell a real duplicate from a stale registry entry.
        self.qdrant = qdrant if qdrant is not None else FakeScopedQdrant()
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
def client(
    tmp_path,
    settings,
    fake_redis,
    fake_qdrant,
    fake_document_storage,
    ingest_queue,
    monkeypatch,
):
    async def fake_verify_token(token):
        if token == "user":
            return AccessToken(
                token=token,
                client_id="frontend",
                scopes=[],
                claims={"sub": "u1", "preferred_username": "user"},
            )
        if token == "service":
            return AccessToken(
                token=token,
                client_id="service",
                scopes=[],
                claims={
                    "sub": "service-subject",
                    "preferred_username": "service-account-test",
                },
            )
        return None

    monkeypatch.setattr(keycloak_token_verifier, "verify_token", fake_verify_token)

    redis_client = RedisClient(settings)
    index_registry = UserIndexRegistry(redis_client, prefix=settings.registry_prefix)
    user_index_service = UserIndexService(
        fake_qdrant,
        redis_client,
        index_registry,
        settings,
        storage=fake_document_storage,
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
    fake_jobs = FakeJobs()
    app = FastAPI()
    app.state.fake_jobs = fake_jobs
    app.state.ingest_queue = ingest_queue
    app.state.qdrant = fake_qdrant
    app.state.redis = redis_client
    app.state.settings = upload_settings
    app.include_router(user_documents_router)
    app.dependency_overrides[Dependencies.get_settings] = lambda: upload_settings
    app.dependency_overrides[Dependencies.get_parser] = lambda: FakeParser()
    app.dependency_overrides[Dependencies.get_redis] = lambda: redis_client
    app.dependency_overrides[Dependencies.get_user_index_registry] = (
        lambda: index_registry
    )
    app.dependency_overrides[Dependencies.get_user_index_service] = (
        lambda: user_index_service
    )
    app.dependency_overrides[Dependencies.get_qdrant] = lambda: fake_qdrant
    app.dependency_overrides[Dependencies.get_user_document_storage] = (
        lambda: fake_document_storage
    )
    app.dependency_overrides[Dependencies.get_jobs] = lambda: fake_jobs
    app.dependency_overrides[Dependencies.get_ingest_queue] = lambda: ingest_queue
    app.dependency_overrides[Dependencies.get_urban_api] = lambda: FakeUrbanApi()
    app.dependency_overrides[Dependencies.get_territory] = lambda: FakeTerritory()
    with TestClient(
        app,
        headers={"Authorization": "Bearer user"},
    ) as c:
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
    def test_project_id_is_the_only_required_document_scope(self, client):
        c, fake_ingestion, index_registry, _ = client
        resp = c.post(
            "/user-documents",
            files={"file": ("doc.docx", b"data")},
            data={"user_id": "u1", "project_id": "p1"},
        )
        assert resp.status_code == 202
        assert index_registry.list_for_user("u1") == []
        assert c.app.state.ingest_queue.pending()

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
        [entry] = c.app.state.ingest_queue.pending()
        assert entry["operation"] == "upload"
        assert entry["scope"] == {
            "user_id": "u1",
            "project_id": "p1",
            "scenario_id": "s1",
        }, "a worker rebuilds the user-scoped service from this"

    def test_job_records_user_and_project_ownership(self, client):
        c, _, _, _ = client
        resp = c.post(
            "/user-documents",
            files={"file": ("doc.docx", b"data")},
            data={"scenario_id": "s1", "project_id": "p1"},
        )

        job = c.app.state.fake_jobs.get(resp.json()["job_id"])
        assert job["user_id"] == "u1"
        assert job["project_id"] == "p1"
        assert job["scenario_id"] == "s1"

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
        [entry] = c.app.state.ingest_queue.pending()
        assert entry["source_object_key"] == storage.upload_calls[-1]

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


class TestUserDocumentJobStatus:
    def test_returns_owned_job_snapshot(self, client):
        c, _, _, _ = client
        c.app.state.fake_jobs.set(
            "job-1",
            {
                "job_id": "job-1",
                "user_id": "u1",
                "status": "processing",
                "stage": "embeddings",
                "stage_index": 6,
                "stage_total": 7,
                "task_progress": 50,
                "overall_progress": 72,
            },
        )

        resp = c.get("/user-documents/jobs/job-1")

        assert resp.status_code == 200
        assert resp.json()["overall_progress"] == 72
        assert "user_id" not in resp.json()

    def test_hides_another_users_job(self, client):
        c, _, _, _ = client
        c.app.state.fake_jobs.set(
            "job-1",
            {"job_id": "job-1", "user_id": "u1", "status": "queued"},
        )

        resp = c.get(
            "/user-documents/jobs/job-1",
            headers={"Authorization": "Bearer service", "X-User-Id": "u2"},
        )

        assert resp.status_code == 404


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

    def test_reload_does_not_require_scenario_index_registry(self, client):
        c, _, _, _ = client
        resp = c.put(
            "/user-documents/doc",
            files={"file": ("doc.docx", b"data")},
            params={"user_id": "u1", "scenario_id": "ghost"},
        )
        assert resp.status_code == 202

    def test_reload_queues_a_durable_job(self, client):
        c, _, _, _ = client
        self._create_index(c)
        resp = c.put(
            "/user-documents/doc",
            files={"file": ("doc.docx", b"data")},
            params={"user_id": "u1", "scenario_id": "s1"},
        )
        assert resp.status_code == 202
        assert [e["operation"] for e in c.app.state.ingest_queue.pending()] == [
            "reload"
        ]

    def test_delete_does_not_require_scenario_index_registry(self, client):
        c, _, _, _ = client
        resp = c.delete(
            "/user-documents/doc", params={"user_id": "u1", "scenario_id": "ghost"}
        )
        assert resp.status_code == 200

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
        resp = c.get("/user-documents", params={"user_id": "u1", "scenario_id": "s1"})
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "documents": []}

    def test_accepts_project_id_directly(self, client):
        c, _, _, _ = client
        resp = c.get("/user-documents", params={"user_id": "u1", "project_id": "p1"})
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "documents": []}


class TestListAvailableUserDocuments:
    @staticmethod
    def _seed(c):
        from qdrant_client.models import PointStruct

        settings = c.app.state.settings
        registry = DocumentRegistry(
            c.app.state.redis,
            prefix=f"{settings.registry_prefix}:user:u1:project:p1",
        )
        registry.register_document(
            "d1",
            {
                "doc_id": "d1",
                "name": "Закон Ленобласти",
                "title": "Региональный закон",
                "version": "2026",
                "source_object_key": "d1.docx",
            },
        )
        c.app.state.qdrant.upsert(
            [
                PointStruct(
                    id="point-d1",
                    vector=[0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "Закон Ленобласти",
                        "version": "2026",
                        "user_id": "u1",
                        "project_id": "p1",
                        "document_level": "regional",
                        "territory_id": 1,
                        "territory_name": "Ленинградская область",
                        "territory_path": [0, 1],
                    },
                )
            ]
        )

    def test_requires_project_id(self, client):
        c, _, _, _ = client
        assert c.get("/user-documents/available").status_code == 422

    def test_returns_documents_applicable_to_territory(self, client):
        c, _, _, _ = client
        self._seed(c)

        resp = c.get(
            "/user-documents/available",
            params={"project_id": "p1", "territory_ids": [54]},
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "count": 1,
            "documents": [
                {
                    "doc_id": "d1",
                    "name": "Закон Ленобласти",
                    "title": "Региональный закон",
                    "version": "2026",
                    "source_file_url": (
                        "/user-documents/%D0%97%D0%B0%D0%BA%D0%BE%D0%BD%20"
                        "%D0%9B%D0%B5%D0%BD%D0%BE%D0%B1%D0%BB%D0%B0%D1%81%D1%82%D0%B8/source"
                        "?user_id=u1&project_id=p1&version=2026"
                    ),
                    "document_level": "regional",
                    "territory_id": 1,
                    "territory_name": "Ленинградская область",
                }
            ],
        }

    def test_unknown_project_returns_empty_list(self, client):
        c, _, _, _ = client
        self._seed(c)

        resp = c.get("/user-documents/available", params={"project_id": "unknown"})

        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "documents": []}


class TestUpdateUserDocumentMetadata:
    @staticmethod
    def _seed(c):
        from qdrant_client.models import PointStruct

        settings = c.app.state.settings
        registry = DocumentRegistry(
            c.app.state.redis,
            prefix=f"{settings.registry_prefix}:user:u1:project:p1",
        )
        registry.register_document(
            "d1",
            {
                "doc_id": "d1",
                "name": "СП 1",
                "title": "Старый заголовок",
                "version": "2026",
            },
        )
        c.app.state.qdrant.upsert(
            [
                PointStruct(
                    id="p1-a",
                    vector=[0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "СП 1",
                        "version": "2026",
                        "user_id": "u1",
                        "project_id": "p1",
                        "title": "Старый заголовок",
                    },
                ),
                PointStruct(
                    id="p1-b",
                    vector=[0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "СП 1",
                        "version": "2026",
                        "user_id": "u1",
                        "project_id": "p1",
                        "title": "Старый заголовок",
                    },
                ),
                PointStruct(
                    id="p2-a",
                    vector=[0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "СП 1",
                        "version": "2026",
                        "user_id": "u1",
                        "project_id": "p2",
                        "title": "Чужой проект",
                    },
                ),
            ]
        )
        return registry

    def test_updates_all_editable_fields_only_in_requested_project(self, client):
        c, _, _, _ = client
        registry = self._seed(c)

        response = c.patch(
            "/user-documents/d1/metadata",
            params={"project_id": "p1"},
            json={
                "title": "Новый заголовок",
                "doc_type": "regulation",
                "corpus": "project",
                "lang": "ru",
                "status": "active",
                "effective_date": "2026-09-01",
                "external_ids": {"code": "MANUAL-1"},
                "metadata": {"owner": "user"},
                "tags": ["проверено"],
                "territory_id": 54,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["doc_id"] == "d1"
        assert body["points_updated"] == 2
        assert set(body["fields_updated"]) >= {
            "title",
            "doc_type",
            "corpus",
            "lang",
            "status",
            "effective_date",
            "external_ids",
            "metadata",
            "tags",
            "territory_id",
            "territory_name",
            "document_level",
        }
        points = c.app.state.qdrant.points
        assert points["p1-a"][1]["title"] == "Новый заголовок"
        assert points["p1-b"][1]["territory_id"] == 54
        assert "MANUAL-1" in points["p1-a"][1]["lookup_keys"]
        assert points["p2-a"][1]["title"] == "Чужой проект"
        assert registry.get_document("d1")["metadata"] == {"owner": "user"}

    def test_requires_project_id(self, client):
        c, _, _, _ = client
        assert (
            c.patch(
                "/user-documents/d1/metadata", json={"title": "Новый заголовок"}
            ).status_code
            == 422
        )

    def test_wrong_project_is_hidden_as_not_found(self, client):
        c, _, _, _ = client
        self._seed(c)

        response = c.patch(
            "/user-documents/d1/metadata",
            params={"project_id": "p2-missing"},
            json={"title": "Новый заголовок"},
        )

        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("error", "expected_status"),
        [
            (TerritoryNotFound("54"), 404),
            (UrbanApiError("connection refused"), 502),
        ],
    )
    def test_maps_territory_errors(self, client, error, expected_status):
        c, _, _, _ = client
        self._seed(c)

        class BrokenTerritory:
            def by_territory_id(self, _territory_id):
                raise error

        c.app.dependency_overrides[Dependencies.get_territory] = BrokenTerritory
        response = c.patch(
            "/user-documents/d1/metadata",
            params={"project_id": "p1"},
            json={"territory_id": 54},
        )

        assert response.status_code == expected_status

    def test_explicit_null_clears_territory(self, client):
        c, _, _, _ = client
        self._seed(c)

        response = c.patch(
            "/user-documents/d1/metadata",
            params={"project_id": "p1"},
            json={"territory_id": None},
        )

        assert response.status_code == 200
        payload = c.app.state.qdrant.points["p1-a"][1]
        assert payload["territory_id"] is None
        assert payload["tagging_status"] == "pending"


@pytest.mark.asyncio
async def test_update_user_document_metadata_handler_uses_project_scope(
    settings, fake_redis, fake_qdrant, monkeypatch
):
    """Exercise the handler without Starlette's synchronous TestClient boundary."""
    from qdrant_client.models import PointStruct

    import src.dvd_service.routers.user_documents as router_mod
    from src.dvd_service.dto import DocumentUpdateRequest

    async def direct_call(function, *args):
        return function(*args)

    monkeypatch.setattr(router_mod, "run_in_threadpool", direct_call)

    redis = RedisClient(settings)
    registry = DocumentRegistry(
        redis, prefix=f"{settings.registry_prefix}:user:u1:project:p1"
    )
    registry.register_document(
        "d1", {"doc_id": "d1", "name": "СП 1", "version": "2026", "title": "old"}
    )
    fake_qdrant.upsert(
        [
            PointStruct(
                id="handler-p1",
                vector=[0.0],
                payload={
                    "doc_id": "d1",
                    "name": "СП 1",
                    "version": "2026",
                    "user_id": "u1",
                    "project_id": "p1",
                    "title": "old",
                },
            ),
            PointStruct(
                id="handler-p2",
                vector=[0.0],
                payload={
                    "doc_id": "d1",
                    "name": "СП 1",
                    "version": "2026",
                    "user_id": "u1",
                    "project_id": "p2",
                    "title": "other",
                },
            ),
        ]
    )

    response = await router_mod.update_user_document_metadata(
        doc_id="d1",
        body=DocumentUpdateRequest(title="new"),
        project_id="p1",
        qdrant=fake_qdrant,
        redis=redis,
        settings=settings,
        territory=FakeTerritory(),
        user_id="u1",
    )

    assert response.points_updated == 1
    assert fake_qdrant.points["handler-p1"][1]["title"] == "new"
    assert fake_qdrant.points["handler-p2"][1]["title"] == "other"


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
                        "project_id": "p1",
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
        self._seed_point(
            fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi"
        )

        resp = c.get(
            "/user-documents/doc/source", params={"user_id": "u1", "scenario_id": "s1"}
        )
        assert resp.status_code == 200
        assert resp.content == b"hi"

    def test_different_user_gets_404(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._seed_point(
            fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi"
        )

        resp = c.get(
            "/user-documents/doc/source",
            params={"scenario_id": "s1"},
            headers={
                "Authorization": "Bearer service",
                "X-User-Id": "u2",
            },
        )
        assert resp.status_code == 404

    def test_inherited_document_downloads_via_child_scenario(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._create_index(c, "u1", "s2", parent_scenario_id="s1")
        self._seed_point(
            fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi"
        )

        resp = c.get(
            "/user-documents/doc/source", params={"user_id": "u1", "scenario_id": "s2"}
        )
        assert resp.status_code == 200
        assert resp.content == b"hi"

    def test_include_inherited_is_noop_for_project_scope(self, client, fake_qdrant):
        c, _, _, storage = client
        self._create_index(c, "u1", "s1")
        self._create_index(c, "u1", "s2", parent_scenario_id="s1")
        self._seed_point(
            fake_qdrant, storage, user_id="u1", scenario_id="s1", key="k", data=b"hi"
        )

        resp = c.get(
            "/user-documents/doc/source",
            params={"user_id": "u1", "scenario_id": "s2", "include_inherited": False},
        )
        assert resp.status_code == 200
        assert resp.content == b"hi"
