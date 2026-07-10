"""HTTP contracts for manual document and fragment editing."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dependencies import Dependencies
from src.dvd_service.dto import DocumentUpdateResponse
from src.dvd_service.routers import library_router


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
