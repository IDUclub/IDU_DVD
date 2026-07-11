"""Unit tests for src/mcp_server/server — MCP tool wiring.

Covers: the shared ``_search`` helper resolves the SearchService through the Dependencies
singleton and forwards the requested kind; the server is named and exposes its tools.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

import src.mcp_server.server as server
from src.dependencies import Dependencies
from src.dvd_service.dto import (
    DeleteResponse,
    DocumentListResponse,
    SearchHit,
    SearchResponse,
    TagsResponse,
    UserIndexDeleteResponse,
    UserIndexInfo,
    UserIndexListResponse,
)


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


def _set_singleton_with_tags(fake_tags) -> None:
    fields = {n: object() for n in Dependencies._FIELDS}
    fields["tags"] = fake_tags
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

    def test_forwards_user_scope_fields(self):
        fake = FakeSearch()
        _set_singleton_with_search(fake)
        server.search_texts(
            "q", user_id="u1", project_id="p1", scenario_id="s1", include_shared=False
        )
        req, kind = fake.calls[-1]
        assert kind == "text"
        assert req.user_id == "u1" and req.project_id == "p1"
        assert req.scenario_id == "s1" and req.include_shared is False


class TestUserIndexSearchTools:
    @pytest.mark.parametrize(
        "tool,expected_kind",
        [
            (server.search_user_index_texts, "text"),
            (server.search_user_index_tables, "table"),
            (server.search_user_index_all, None),
        ],
    )
    def test_forces_index_only_scope(self, tool, expected_kind):
        fake = FakeSearch()
        _set_singleton_with_search(fake)
        tool("u1", "s1", "q")
        req, kind = fake.calls[-1]
        assert kind == expected_kind
        assert req.user_id == "u1" and req.scenario_id == "s1"
        assert req.include_shared is False


class FakeTags:
    def get_tags(self):
        return TagsResponse(count=1, tags=["alpha"])


class FakeUserIndexService:
    def __init__(self):
        self.create_calls = []
        self.delete_calls = []

    def create_index(self, user_id, scenario_id, project_id, parent_scenario_id=None):
        self.create_calls.append((user_id, scenario_id, project_id, parent_scenario_id))
        if parent_scenario_id == "boom":
            raise ValueError("bad parent")
        return UserIndexInfo(
            user_id=user_id,
            scenario_id=scenario_id,
            project_id=project_id,
            parent_scenario_id=parent_scenario_id,
            created_at="2026-01-01T00:00:00",
            document_count=0,
        )

    def list_indices(self, user_id):
        return UserIndexListResponse(count=0, indices=[])

    def delete_index(self, user_id, scenario_id):
        self.delete_calls.append((user_id, scenario_id))
        if scenario_id == "ghost":
            raise KeyError(f"index not found: {user_id}/{scenario_id}")
        return UserIndexDeleteResponse(
            user_id=user_id, scenario_id=scenario_id, points_deleted=1
        )


def _set_singleton_with_user_index_service(fake) -> None:
    fields = {n: object() for n in Dependencies._FIELDS}
    fields["user_index_service"] = fake
    Dependencies().set(**fields)


class TestUserIndexManagementTools:
    def test_create_user_index_delegates(self):
        fake = FakeUserIndexService()
        _set_singleton_with_user_index_service(fake)
        info = server.create_user_index("u1", "s1", "p1", parent_scenario_id="s0")
        assert info.scenario_id == "s1"
        assert fake.create_calls[-1] == ("u1", "s1", "p1", "s0")

    def test_create_user_index_wraps_value_error_as_tool_error(self):
        fake = FakeUserIndexService()
        _set_singleton_with_user_index_service(fake)
        with pytest.raises(ToolError):
            server.create_user_index("u1", "s1", "p1", parent_scenario_id="boom")

    def test_list_user_indices_delegates(self):
        fake = FakeUserIndexService()
        _set_singleton_with_user_index_service(fake)
        resp = server.list_user_indices("u1")
        assert resp.count == 0

    def test_delete_user_index_delegates(self):
        fake = FakeUserIndexService()
        _set_singleton_with_user_index_service(fake)
        resp = server.delete_user_index("u1", "s1")
        assert resp.points_deleted == 1
        assert fake.delete_calls[-1] == ("u1", "s1")

    def test_delete_user_index_wraps_key_error_as_tool_error(self):
        fake = FakeUserIndexService()
        _set_singleton_with_user_index_service(fake)
        with pytest.raises(ToolError):
            server.delete_user_index("u1", "ghost")


class TestListDocumentsTool:
    def test_forwards_filters(self):
        fake = FakeDocuments()
        _set_singleton_with_documents(fake)
        resp = server.list_documents(name="СП 1", block="amendment")
        assert resp.count == 0
        assert fake.calls[-1] == ("СП 1", None, "amendment", None, None, None)


class TestGetTagsTool:
    def test_returns_tags(self):
        fake = FakeTags()
        _set_singleton_with_tags(fake)
        resp = server.get_tags()
        assert resp.count == 1 and resp.tags == ["alpha"]


def _set_full_singleton(**overrides) -> None:
    fields = {n: object() for n in Dependencies._FIELDS}
    fields.update(overrides)
    Dependencies().set(**fields)


class TestListUserDocumentsTool:
    def test_scopes_to_ancestor_chain_by_default(
        self, settings, fake_redis, fake_qdrant, user_index_registry
    ):
        user_index_registry.create("u1", "s1", "p1")
        user_index_registry.create("u1", "s2", "p1", parent_scenario_id="s1")
        _set_full_singleton(qdrant=fake_qdrant, user_index_registry=user_index_registry)

        resp = server.list_user_documents("u1", "s2")

        assert resp == DocumentListResponse(count=0, documents=[])

    def test_include_inherited_false_limits_to_own_scenario(
        self, settings, fake_redis, fake_qdrant, user_index_registry, monkeypatch
    ):
        user_index_registry.create("u1", "s1", "p1")
        user_index_registry.create("u1", "s2", "p1", parent_scenario_id="s1")
        _set_full_singleton(qdrant=fake_qdrant, user_index_registry=user_index_registry)

        captured = {}
        from src.dvd_service.services.dvd_service import DocumentsService

        real_list_documents = DocumentsService.list_documents

        def _spy(self, *a, **k):
            captured.update(k)
            return real_list_documents(self, *a, **k)

        monkeypatch.setattr(DocumentsService, "list_documents", _spy)
        server.list_user_documents("u1", "s2", include_inherited=False)
        assert captured["scenario_ids"] == ["s2"]


class TestDeleteUserDocumentTool:
    def test_unknown_index_raises_tool_error(
        self, settings, fake_redis, user_index_registry
    ):
        _set_full_singleton(user_index_registry=user_index_registry)
        with pytest.raises(ToolError):
            server.delete_user_document("u1", "ghost", "doc")

    def test_delegates_to_scoped_ingestion(
        self, settings, fake_redis, fake_qdrant, user_index_registry, monkeypatch
    ):
        user_index_registry.create("u1", "s1", "p1")
        _set_full_singleton(qdrant=fake_qdrant, user_index_registry=user_index_registry)

        class FakeIngestion:
            def delete_document(self, name, version):
                assert name == "doc" and version is None
                return {
                    "name": "doc",
                    "versions_removed": ["v1"],
                    "points_deleted": 1,
                    "points_updated": 0,
                }

        monkeypatch.setattr(
            server, "build_user_ingestion_from_deps", lambda *a, **k: FakeIngestion()
        )
        resp = server.delete_user_document("u1", "s1", "doc")
        assert resp == DeleteResponse(
            name="doc", versions_removed=["v1"], points_deleted=1, points_updated=0
        )

    def test_unknown_document_raises_tool_error(
        self, settings, fake_redis, fake_qdrant, user_index_registry, monkeypatch
    ):
        user_index_registry.create("u1", "s1", "p1")
        _set_full_singleton(qdrant=fake_qdrant, user_index_registry=user_index_registry)

        class FakeIngestion:
            def delete_document(self, name, version):
                raise KeyError("документ не найден: doc")

        monkeypatch.setattr(
            server, "build_user_ingestion_from_deps", lambda *a, **k: FakeIngestion()
        )
        with pytest.raises(ToolError):
            server.delete_user_document("u1", "s1", "doc")


class TestServerObject:
    def test_named(self):
        assert server.mcp.name == "dvd-idu"

    def test_tools_registered(self):
        for tool in (
            "search_texts",
            "search_tables",
            "search_all",
            "search_user_index_texts",
            "search_user_index_tables",
            "search_user_index_all",
            "job_status",
            "document_versions",
            "pending_references",
            "list_documents",
            "get_tags",
            "create_user_index",
            "list_user_indices",
            "delete_user_index",
            "list_user_documents",
            "delete_user_document",
        ):
            assert hasattr(server, tool)
