"""Unit tests for src/mcp_server/server — MCP tool wiring.

Covers: the shared ``_search`` helper resolves the SearchService through the Dependencies
singleton and forwards the requested kind; the server is named and exposes its tools.
"""

from __future__ import annotations

import pytest

import src.mcp_server.server as server
from src.dependencies import Dependencies
from src.dvd_service.dto import DocumentListResponse, SearchHit, SearchResponse


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


@pytest.fixture(autouse=True)
def _reset_singleton():
    Dependencies.reset()
    yield
    Dependencies.reset()


def _set_singleton_with_search(fake_search) -> None:
    fields = {n: object() for n in Dependencies._FIELDS}
    fields["search"] = fake_search
    Dependencies().set(**fields)


def _set_singleton_with_documents(fake_documents) -> None:
    fields = {n: object() for n in Dependencies._FIELDS}
    fields["documents"] = fake_documents
    Dependencies().set(**fields)


class TestSearchHelper:
    def test_forwards_kind_and_query(self):
        fake = FakeSearch()
        _set_singleton_with_search(fake)
        resp = server._search("требования", None, None, None, 5, 0, "table")
        assert resp.count == 1
        req, kind = fake.calls[-1]
        assert kind == "table"
        assert req.query == "требования" and req.limit == 5

    def test_raises_before_dependencies_initialized(self):
        with pytest.raises(RuntimeError):
            server._search("q", None, None, None, 5, 0, None)


class TestListDocumentsTool:
    def test_forwards_filters(self):
        fake = FakeDocuments()
        _set_singleton_with_documents(fake)
        resp = server.list_documents(name="СП 1", block="amendment")
        assert resp.count == 0
        assert fake.calls[-1] == ("СП 1", None, "amendment", None, None, None)


class TestServerObject:
    def test_named(self):
        assert server.mcp.name == "dvd-idu"

    def test_tools_registered(self):
        for tool in (
            "search_texts",
            "search_tables",
            "search_all",
            "job_status",
            "document_versions",
            "pending_references",
            "list_documents",
        ):
            assert hasattr(server, tool)
