"""Unit tests for src/dvd_service/routers — HTTP endpoints.

Builds a FastAPI app from the router and overrides each per-dependency getter with a fake, so
the endpoints are tested in isolation (no Qdrant/Redis/Ollama). Covers: upload (queued / duplicate
/ unsupported type), job status (found / missing), and the three search endpoints.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.common.config import Settings
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DocumentListResponse,
    SearchHit,
    SearchResponse,
    TagsResponse,
)
from src.dvd_service.routers import documents_router, search_router


class FakeParser:
    def extract_raw(self, path):
        return [{"text": "x", "category": "NarrativeText", "html": None}]

    def content_hash(self, raw):
        return "hash-1"


class FakeRegistry:
    def __init__(self):
        self.dup = False
        self.names = {"Известный документ"}

    def has_hash(self, h):
        return self.dup

    def hash_info(self, h):
        return {"name": "N", "version": "V"}

    def has_name(self, name):
        return name in self.names


class FakeJobs:
    def __init__(self):
        self.store = {}

    def set(self, jid, data):
        self.store[jid] = data

    def get(self, jid):
        return self.store.get(jid)

    def active(self):
        return [
            job
            for job in self.store.values()
            if job.get("status") in {"queued", "processing"}
        ]


class FakeIngestion:
    def __init__(self):
        self.calls = []
        self.update_calls = []
        self.reload_calls = []
        self.delete_calls = []

    def ingest(self, *a, **k):
        self.calls.append((a, k))
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
            "versions_removed": [version] if version else ["v1", "v2"],
            "points_deleted": 3,
            "points_updated": 2,
        }


class FakeSearch:
    def __init__(self):
        self.calls = []

    def search(self, req, kind):
        self.calls.append((req, kind))
        return SearchResponse(
            count=1,
            hits=[
                SearchHit(
                    id="1",
                    score=0.5,
                    doc_id="d",
                    name="n",
                    version="v",
                    kind="text",
                    type="clause",
                    text="hello",
                )
            ],
        )


class FakeDocuments:
    def __init__(self):
        self.calls = []

    def list_documents(self, name, version, block, tags, uploaded_from, uploaded_to):
        self.calls.append((name, version, block, tags, uploaded_from, uploaded_to))
        return DocumentListResponse(count=0, documents=[])


class FakeTags:
    def __init__(self):
        self.calls = []

    def get_tags(self):
        self.calls.append(())
        return TagsResponse(count=2, tags=["fire", "water"])


@pytest.fixture
def client(tmp_path, fake_qdrant, fake_document_storage):
    fakes = {
        "settings": Settings(upload_dir=str(tmp_path)),
        "parser": FakeParser(),
        "registry": FakeRegistry(),
        "jobs": FakeJobs(),
        "ingestion": FakeIngestion(),
        "search": FakeSearch(),
        "documents": FakeDocuments(),
        "tags": FakeTags(),
        "qdrant": fake_qdrant,
        "document_storage": fake_document_storage,
    }
    app = FastAPI()
    app.include_router(documents_router)
    app.include_router(search_router)
    app.dependency_overrides[Dependencies.get_settings] = lambda: fakes["settings"]
    app.dependency_overrides[Dependencies.get_parser] = lambda: fakes["parser"]
    app.dependency_overrides[Dependencies.get_registry] = lambda: fakes["registry"]
    app.dependency_overrides[Dependencies.get_jobs] = lambda: fakes["jobs"]
    app.dependency_overrides[Dependencies.get_ingestion] = lambda: fakes["ingestion"]
    app.dependency_overrides[Dependencies.get_search] = lambda: fakes["search"]
    app.dependency_overrides[Dependencies.get_documents] = lambda: fakes["documents"]
    app.dependency_overrides[Dependencies.get_tags] = lambda: fakes["tags"]
    app.dependency_overrides[Dependencies.get_qdrant] = lambda: fakes["qdrant"]
    app.dependency_overrides[Dependencies.get_document_storage] = lambda: fakes[
        "document_storage"
    ]
    with TestClient(app) as c:
        yield c, fakes


class TestUpload:
    def test_queues_background_ingest(self, client):
        c, fakes = client
        resp = c.post("/documents", files={"file": ("doc.docx", b"data")})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["job_id"] in fakes["jobs"].store
        assert fakes["ingestion"].calls, "background ingest must have run"

    def test_exact_duplicate_rejected(self, client):
        c, fakes = client
        fakes["registry"].dup = True
        resp = c.post("/documents", files={"file": ("doc.docx", b"data")})
        assert resp.status_code == 400

    def test_unsupported_extension_rejected(self, client):
        c, _ = client
        resp = c.post("/documents", files={"file": ("scan.pdf", b"data")})
        assert resp.status_code == 415

    def test_manual_name_and_version_forwarded(self, client):
        c, fakes = client
        resp = c.post(
            "/documents",
            files={"file": ("doc.docx", b"data")},
            data={"name": "СП 5.2025", "version": "2025"},
        )
        assert resp.status_code == 202
        _, kwargs = fakes["ingestion"].calls[-1]
        assert kwargs["name_override"] == "СП 5.2025"
        assert kwargs["version_override"] == "2025"

    def test_saves_source_to_minio_and_forwards_object_key(self, client):
        c, fakes = client
        resp = c.post("/documents", files={"file": ("doc.docx", b"data")})
        assert resp.status_code == 202
        assert fakes["document_storage"].upload_calls  # saved before the job was queued
        _, kwargs = fakes["ingestion"].calls[-1]
        assert kwargs["source_object_key"] == fakes["document_storage"].upload_calls[-1]

    def test_storage_failure_rejects_upload_and_never_queues_a_job(self, client):
        c, fakes = client
        fakes["document_storage"].fail_upload = True
        resp = c.post("/documents", files={"file": ("doc.docx", b"data")})
        assert resp.status_code == 502
        assert not fakes["ingestion"].calls


class TestUpdateDocument:
    def test_queues_background_update(self, client):
        c, fakes = client
        resp = c.patch(
            "/documents/Известный документ",
            files={"file": ("doc.docx", b"data")},
            data={"version": "ред. 2"},
        )
        assert resp.status_code == 202 and resp.json()["status"] == "queued"
        args, kwargs = fakes["ingestion"].update_calls[-1]
        assert args[0] == "Известный документ"
        assert kwargs["version_override"] == "ред. 2"

    def test_unknown_name_returns_404(self, client):
        c, fakes = client
        resp = c.patch("/documents/нет такого", files={"file": ("doc.docx", b"data")})
        assert resp.status_code == 404
        assert not fakes["ingestion"].update_calls

    def test_exact_duplicate_rejected(self, client):
        c, fakes = client
        fakes["registry"].dup = True
        resp = c.patch(
            "/documents/Известный документ", files={"file": ("doc.docx", b"data")}
        )
        assert resp.status_code == 400

    def test_unsupported_extension_rejected(self, client):
        c, _ = client
        resp = c.patch(
            "/documents/Известный документ", files={"file": ("scan.pdf", b"data")}
        )
        assert resp.status_code == 415


class TestReloadDocument:
    def test_queues_background_reload(self, client):
        c, fakes = client
        resp = c.put(
            "/documents/Известный документ", files={"file": ("doc.docx", b"data")}
        )
        assert resp.status_code == 202 and resp.json()["status"] == "queued"
        args, _ = fakes["ingestion"].reload_calls[-1]
        assert args[0] == "Известный документ"

    def test_duplicate_not_rejected(self, client):
        c, fakes = client
        fakes["registry"].dup = True  # PUT rebuilds the index, so re-upload is fine
        resp = c.put(
            "/documents/Известный документ", files={"file": ("doc.docx", b"data")}
        )
        assert resp.status_code == 202


class TestDeleteDocument:
    def test_deletes_all_versions(self, client):
        c, fakes = client
        resp = c.delete("/documents/Известный документ")
        assert resp.status_code == 200
        body = resp.json()
        assert body["versions_removed"] == ["v1", "v2"]
        assert body["points_deleted"] == 3
        assert fakes["ingestion"].delete_calls[-1] == ("Известный документ", None)

    def test_deletes_single_version(self, client):
        c, fakes = client
        resp = c.delete("/documents/Известный документ", params={"version": "v2"})
        assert resp.status_code == 200
        assert resp.json()["versions_removed"] == ["v2"]
        assert fakes["ingestion"].delete_calls[-1] == ("Известный документ", "v2")

    def test_unknown_name_returns_404(self, client):
        c, _ = client
        assert c.delete("/documents/нет такого").status_code == 404


class TestDownloadSource:
    def _seed(
        self, fakes, *, name="СП 1", version="v1", key="obj-key.docx", data=b"hi"
    ):
        from qdrant_client.models import PointStruct

        fakes["qdrant"].upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={
                        "name": name,
                        "version": version,
                        "source_object_key": key,
                    },
                )
            ]
        )
        fakes["document_storage"].upload(key, data, "application/octet-stream")

    def test_downloads_the_original_file(self, client):
        c, fakes = client
        self._seed(fakes)
        resp = c.get("/documents/СП 1/source")
        assert resp.status_code == 200
        assert resp.content == b"hi"
        assert "attachment" in resp.headers["content-disposition"]

    def test_unknown_document_returns_404(self, client):
        c, _ = client
        assert c.get("/documents/нет такого/source").status_code == 404

    def test_unknown_version_returns_404(self, client):
        c, fakes = client
        self._seed(fakes)
        resp = c.get("/documents/СП 1/source", params={"version": "nope"})
        assert resp.status_code == 404

    def test_document_without_stored_source_returns_404(self, client):
        c, fakes = client
        from qdrant_client.models import PointStruct

        fakes["qdrant"].upsert(
            [
                PointStruct(
                    id="pt1", vector=[0.0], payload={"name": "СП 1", "version": "v1"}
                )
            ]
        )
        assert c.get("/documents/СП 1/source").status_code == 404

    def test_missing_object_in_storage_returns_404(self, client):
        c, fakes = client
        from qdrant_client.models import PointStruct

        fakes["qdrant"].upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={
                        "name": "СП 1",
                        "version": "v1",
                        "source_object_key": "ghost",
                    },
                )
            ]
        )
        assert c.get("/documents/СП 1/source").status_code == 404

    def test_defaults_to_latest_version(self, client):
        c, fakes = client
        self._seed(fakes, version="v1", key="key-v1.docx", data=b"old")
        self._seed(fakes, version="v2", key="key-v2.docx", data=b"new")
        resp = c.get("/documents/СП 1/source")
        assert resp.status_code == 200
        assert resp.content == b"new"


class TestListDocuments:
    def test_forwards_filters_to_service(self, client):
        c, fakes = client
        resp = c.get(
            "/documents",
            params={"name": "СП 1", "block": "amendment", "tags": ["a", "b"]},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        assert fakes["documents"].calls[-1] == (
            "СП 1",
            None,
            "amendment",
            ["a", "b"],
            None,
            None,
        )

    def test_no_filters(self, client):
        c, _ = client
        resp = c.get("/documents")
        assert resp.status_code == 200 and resp.json() == {
            "count": 0,
            "documents": [],
        }


class TestJobStatus:
    def test_lists_active_jobs(self, client):
        c, fakes = client
        fakes["jobs"].set(
            "j1", {"job_id": "j1", "status": "processing", "filename": "a.docx"}
        )
        fakes["jobs"].set("j2", {"job_id": "j2", "status": "done"})
        resp = c.get("/documents/jobs/active")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1 and resp.json()["jobs"][0]["job_id"] == "j1"

    def test_found(self, client):
        c, fakes = client
        fakes["jobs"].set("j1", {"job_id": "j1", "status": "done"})
        resp = c.get("/documents/j1")
        assert resp.status_code == 200 and resp.json()["status"] == "done"

    def test_missing_returns_404(self, client):
        c, _ = client
        assert c.get("/documents/nope").status_code == 404


class TestGetTags:
    def test_returns_tags(self, client):
        c, _ = client
        resp = c.get("/tags")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2 and body["tags"] == ["fire", "water"]

    def test_calls_service_once(self, client):
        c, fakes = client
        c.get("/tags")
        assert len(fakes["tags"].calls) == 1


class TestSearchEndpoints:
    @pytest.mark.parametrize(
        "path,expected_kind",
        [
            ("/search/texts", "text"),
            ("/search/tables", "table"),
            ("/search", None),
        ],
    )
    def test_search_routes_pass_correct_kind(self, client, path, expected_kind):
        c, fakes = client
        resp = c.post(path, json={"query": "требования"})
        assert resp.status_code == 200 and resp.json()["count"] == 1
        assert fakes["search"].calls[-1][1] == expected_kind


class TestUserIndexSearchEndpoints:
    @pytest.mark.parametrize(
        "path,expected_kind",
        [
            ("/search/user-index/texts", "text"),
            ("/search/user-index/tables", "table"),
            ("/search/user-index", None),
        ],
    )
    def test_requires_user_id_and_scenario_id(self, client, path, expected_kind):
        c, _ = client
        resp = c.post(path, json={"query": "требования"})
        assert resp.status_code == 400

    def test_forces_include_shared_false(self, client):
        c, fakes = client
        resp = c.post(
            "/search/user-index/texts",
            json={"query": "требования", "user_id": "u1", "scenario_id": "s1"},
        )
        assert resp.status_code == 200
        req, kind = fakes["search"].calls[-1]
        assert kind == "text"
        assert req.user_id == "u1" and req.scenario_id == "s1"
        assert req.include_shared is False
