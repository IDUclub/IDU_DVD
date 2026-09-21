"""HTTP contracts for manual document and fragment editing."""

from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api_clients.urban_api_client import UrbanApiClient
from src.common.auth import require_admin, require_authenticated
from src.dependencies import Dependencies
from src.dvd_service.dto import DocumentUpdateResponse
from src.dvd_service.modules.territory import TerritoryResolver
from src.dvd_service.routers import library_router
from src.dvd_service.services.dvd_service import DocumentEditorService


class FakeEditor:
    def __init__(self):
        self.document_calls = []
        self.fragment_calls = []

    def update_document(self, doc_id, updates):
        self.document_calls.append((doc_id, updates))
        if doc_id == "missing":
            raise KeyError("document not found")
        return DocumentUpdateResponse(
            doc_id=doc_id,
            points_updated=3,
            fields_updated=sorted(updates),
        )

    def update_fragment(self, doc_id, fragment_id, updates):
        self.fragment_calls.append((doc_id, fragment_id, updates))
        if not updates.get("text", "x").strip():
            raise ValueError("fragment text cannot be empty")
        return {
            "id": fragment_id,
            "order": 1,
            "kind": "text",
            "type": "clause",
            "text": updates.get("text", "old"),
            "tags": updates.get("tags", []),
            "metadata": updates.get("metadata", {}),
        }


def _client():
    editor = FakeEditor()
    app = FastAPI()
    app.include_router(library_router)
    app.dependency_overrides[Dependencies.get_editor] = lambda: editor
    app.dependency_overrides[require_authenticated] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app), editor


def test_document_metadata_patch_forwards_only_supplied_fields():
    with _client()[0] as client:
        editor = client.app.dependency_overrides[Dependencies.get_editor]()
        response = client.patch(
            "/library/documents/doc-1",
            json={"title": "Новый заголовок", "tags": ["ручной"]},
        )
        assert response.status_code == 200
        assert editor.document_calls == [
            ("doc-1", {"title": "Новый заголовок", "tags": ["ручной"]})
        ]


def test_document_metadata_patch_accepts_version_and_territory():
    client, editor = _client()
    body = {"current_version": "2025", "version": "2026", "territory_id": 54}
    with client:
        response = client.patch("/library/documents/doc-1", json=body)
    assert response.status_code == 200
    assert editor.document_calls == [("doc-1", body)]


@pytest.mark.parametrize(
    "urban_status, expected_status", [(200, 200), (403, 502), (422, 502), (404, 404)]
)
def test_territory_patch_resolves_before_writing(
    urban_status, expected_status, fake_qdrant
):
    original = {"doc_id": "doc-1", "name": "Name", "version": "v1", "title": "Old"}
    fake_qdrant.points["p"] = ([0.1], original.copy())
    registry = Mock()
    registry.get_document.return_value = original.copy()
    urban = UrbanApiClient(base="http://urban.test/api")
    urban._client.close()
    urban._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                urban_status,
                json={
                    "territory_id": 12639,
                    "name": "Россия",
                    "level": 1,
                    "parent": None,
                },
            )
        )
    )
    editor = DocumentEditorService(
        fake_qdrant, registry, Mock(), TerritoryResolver(urban)
    )
    client, _ = _client()
    client.app.dependency_overrides[Dependencies.get_editor] = lambda: editor
    try:
        with client:
            response = client.patch(
                "/library/documents/doc-1", json={"title": "New", "territory_id": 12639}
            )
        assert response.status_code == expected_status
        if expected_status == 200:
            payload = fake_qdrant.points["p"][1]
            assert payload["title"] == "New"
            assert payload["name"] == "Name"
            assert payload["territory_id"] == 12639
            assert payload["territory_path"] == [12639]
            assert payload["territory_source"] == "manual"
            assert registry.register_document.call_args.args[1]["territory_id"] == 12639
        else:
            assert "Urban API" in response.json()["detail"]
            assert fake_qdrant.points["p"][1] == original
            registry.register_document.assert_not_called()
    finally:
        urban.close()


def test_fragment_patch_returns_edited_fragment():
    client, editor = _client()
    with client:
        response = client.patch(
            "/library/documents/doc-1/fragments/node-1",
            json={"text": "Исправленный текст", "tags": ["проверено"]},
        )
        assert response.status_code == 200
        assert response.json()["text"] == "Исправленный текст"
        assert editor.fragment_calls[-1][1] == "node-1"


def test_missing_document_returns_404():
    with _client()[0] as client:
        response = client.patch("/library/documents/missing", json={"title": "x"})
        assert response.status_code == 404


def test_empty_fragment_text_returns_422():
    with _client()[0] as client:
        response = client.patch(
            "/library/documents/doc-1/fragments/node-1", json={"text": "  "}
        )
        assert response.status_code == 422
