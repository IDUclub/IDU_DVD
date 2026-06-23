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
from src.dvd_service.dto import SearchHit, SearchResponse
from src.dvd_service.routers import documents_router, search_router


class FakeParser:
    def extract_raw(self, path):
        return [{"text": "x", "category": "NarrativeText", "html": None}]

    def content_hash(self, raw):
        return "hash-1"


class FakeRegistry:
    def __init__(self):
        self.dup = False

    def has_hash(self, h):
        return self.dup

    def hash_info(self, h):
        return {"name": "N", "version": "V"}


class FakeJobs:
    def __init__(self):
        self.store = {}

    def set(self, jid, data):
        self.store[jid] = data

    def get(self, jid):
        return self.store.get(jid)


class FakeIngestion:
    def __init__(self):
        self.calls = []

    def ingest(self, *a, **k):
        self.calls.append((a, k))
        return {}


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


@pytest.fixture
def client(tmp_path):
    fakes = {
        "settings": Settings(upload_dir=str(tmp_path)),
        "parser": FakeParser(),
        "registry": FakeRegistry(),
        "jobs": FakeJobs(),
        "ingestion": FakeIngestion(),
        "search": FakeSearch(),
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
        resp = c.post("/documents", files={"file": ("doc.txt", b"data")})
        assert resp.status_code == 415


class TestJobStatus:
    def test_found(self, client):
        c, fakes = client
        fakes["jobs"].set("j1", {"job_id": "j1", "status": "done"})
        resp = c.get("/documents/j1")
        assert resp.status_code == 200 and resp.json()["status"] == "done"

    def test_missing_returns_404(self, client):
        c, _ = client
        assert c.get("/documents/nope").status_code == 404


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
