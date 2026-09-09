"""Real Qdrant filters with a memory-only database; no embeddings for exact retrieval."""

import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models

from src.common.db.qdrant_client import QdrantRepository
from src.dvd_service.dto.fragment_search import (
    FragmentSearchRequest,
    NameBackfillRequest,
)
from src.dvd_service.modules.fragment_structure import (
    StructurePattern,
    annotate_fragments,
)
from src.dvd_service.services.dvd_service import SearchService
from src.dvd_service.services.fragment_search import FragmentSearchService


@pytest.fixture
def fragments(settings):
    repo = QdrantRepository.__new__(QdrantRepository)
    repo.settings = settings
    repo.collection = "fragments"
    repo.client = QdrantClient(":memory:")
    repo.client.create_collection(
        repo.collection,
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
    )
    ids = [str(uuid.uuid4()) for _ in range(7)]
    texts = [
        "Пожарная безопасность",
        "вспучивающееся огнезащитное покрытие: Полное определение.",
        "Первое требование.",
        "Другое определение: Содержание.",
        "Следующий пункт.",
        "Секрет: Недоступно.",
        "Иной документ: Содержание.",
    ]
    nums = ["3", "3.3", "3.3.1", "3.4", "3.30", "3.3", "3.3"]
    for i in range(7):
        payload = dict(
            doc_id="doc" if i < 6 else "other",
            name="СП 2.13130.2020" if i < 6 else "Other",
            version="new",
            versions=["2020", "new"],
            text=texts[i],
            numbering=nums[i],
            kind="text",
            type="section" if i == 0 else "definition",
            block="main",
            order=i,
            parent_id=ids[1] if i == 2 else ids[0] if i in [1, 3, 4] else None,
        )
        if i == 5:
            payload.update(user_id="private", project_id="p")
        repo.client.upsert(
            repo.collection,
            points=[models.PointStruct(id=ids[i], vector=[1.0, 0.0], payload=payload)],
        )
    svc = FragmentSearchService(SearchService(repo, settings, SimpleNamespace()))
    yield svc, ids
    repo.client.close()


def test_exact_reference_finds_definition_and_children_without_vectors(fragments):
    svc, ids = fragments
    response = svc.search(
        FragmentSearchRequest(pattern="3.3", name="СП2.13130.2020 2020", version="2020")
    )
    assert [h.id for h in response.hits] == ids[1:3]
    assert response.hits[0].type == "definition"
    assert response.hits[0].fragment_name == "вспучивающееся огнезащитное покрытие"
    assert response.hits[1].matched is False
    assert response.hits[0].version == "2020"


def test_mask_range_and_exact_boundaries(fragments):
    svc, ids = fragments
    r = svc.search(FragmentSearchRequest(pattern="3.3–3.4", doc_id="doc"))
    assert [h.id for h in r.hits] == ids[1:4]
    r = svc.search(
        FragmentSearchRequest(pattern="3.*", doc_id="doc", include_children=False)
    )
    assert {h.id for h in r.hits} == set(ids[1:5])
    assert not r.ambiguous
    with pytest.raises(ValueError, match="siblings"):
        StructurePattern("3.3–4.2")


def test_parent_names_and_path_selectors(fragments):
    svc, ids = fragments
    r = svc.search(
        FragmentSearchRequest(
            pattern="3.3", name_query="ПОЖАРНАЯ  безопасность", name_scope="path"
        )
    )
    assert [h.id for h in r.hits] == ids[1:3]
    assert (
        svc.search(
            FragmentSearchRequest(
                pattern="3.3", name_query="Пожарная безопасность", name_scope="self"
            )
        ).count
        == 0
    )
    r = svc.search(FragmentSearchRequest(pattern="Пожарная безопасность / 3.3"))
    assert [h.id for h in r.hits] == ids[1:3]


def test_pagination_no_duplicates_and_changed_snapshot_rejected(fragments):
    svc, ids = fragments
    req = FragmentSearchRequest(pattern="3.*", doc_id="doc", limit=1)
    found = []
    while True:
        r = svc.search(req)
        found += [h.id for h in r.hits]
        if r.complete:
            break
        req = req.model_copy(update={"cursor": r.next_cursor})
    assert found == ids[1:5]
    svc.qdrant.set_points_payload([ids[2]], {"text": "Changed"})
    with pytest.raises(ValueError, match="changed"):
        svc.search(req)


def test_ambiguity_preserves_candidates_and_private_scope(fragments):
    svc, ids = fragments
    r = svc.search(FragmentSearchRequest(pattern="3.3", include_children=False))
    assert r.ambiguous and r.match_count == 2
    assert {h.id for h in r.hits} == {ids[1], ids[6]}
    private = svc.search(
        FragmentSearchRequest(
            pattern="3.3", user_id="private", project_id="p", include_shared=False
        )
    )
    assert [h.id for h in private.hits] == [ids[5]]


def test_backfill_preview_resume_idempotence(fragments):
    svc, ids = fragments
    before = svc.qdrant.retrieve(ids)
    preview = svc.backfill(NameBackfillRequest(doc_id="doc", limit=2))
    assert preview["would_update"] == 2
    assert svc.qdrant.retrieve(ids) == before
    req = NameBackfillRequest(doc_id="doc", dry_run=False, limit=2)
    while True:
        page = svc.backfill(req)
        if page["complete"]:
            break
        req = req.model_copy(update={"cursor": page["next_cursor"]})
    assert (
        svc.backfill(NameBackfillRequest(doc_id="doc", dry_run=False))["updated"] == 0
    )
    assert "fragment_name_schema" not in svc.qdrant.retrieve([ids[5]])[ids[5]]
    assert svc.qdrant.get_point(ids[1])[0] == [1.0, 0.0]


def test_only_source_grounded_names_are_kept():
    nodes = annotate_fragments(
        [
            {
                "id": "a",
                "text": "Обычное предложение без заголовка.",
                "fragment_name": "Выдуманный заголовок",
            }
        ]
    )
    assert nodes[0]["fragment_name"] is None


def test_backfill_private_scope_is_explicit_and_isolated(fragments):
    svc, ids = fragments
    with pytest.raises(ValueError, match="both"):
        NameBackfillRequest(user_id="private")
    r = svc.backfill(
        NameBackfillRequest(user_id="private", project_id="p", dry_run=False)
    )
    assert r["updated"] == 1 and r["changes"][0]["id"] == ids[5]
    assert "fragment_name_schema" not in svc.qdrant.retrieve([ids[1]])[ids[1]]


def test_numbered_term_name_is_extracted_from_source():
    node = annotate_fragments(
        [
            {
                "id": "a",
                "numbering": "3.3",
                "text": "3.3 Огнезащитное покрытие: определение",
            }
        ]
    )[0]
    assert node["fragment_name"] == "Огнезащитное покрытие"


def test_name_selection_precedes_loading_corpus_text(fragments, monkeypatch):
    svc, ids = fragments
    scan = svc.qdrant.scan_points
    loaded = []

    def scoped_scan(query_filter):
        nodes = scan(query_filter)
        loaded.extend(nodes)
        return nodes

    monkeypatch.setattr(svc.qdrant, "scan_points", scoped_scan)
    svc.search(FragmentSearchRequest(pattern="3.3", name="СП2.13130.2020"))
    assert {n["doc_id"] for n in loaded} == {"doc"}
    assert ids[5] not in {n["id"] for n in loaded}


def test_rest_structural_search_uses_authenticated_owner(fragments):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.common.auth import get_effective_user_id, require_authenticated
    from src.dependencies import Dependencies
    from src.dvd_service.routers.search import router

    svc, ids = fragments
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[Dependencies.get_search] = lambda: svc.search_service
    app.dependency_overrides[get_effective_user_id] = lambda: "private"
    app.dependency_overrides[require_authenticated] = lambda: None
    with TestClient(app) as client:
        response = client.post(
            "/search/structure",
            json={"pattern": "3.3", "project_id": "p", "include_shared": False},
        )
        assert response.status_code == 200
        assert [h["id"] for h in response.json()["hits"]] == [ids[5]]
        assert (
            client.post(
                "/search/names",
                json={"name_query": "Секрет", "user_id": "another", "project_id": "p"},
            ).status_code
            == 403
        )


def test_mcp_structure_contract_and_owner_pinning(fragments, monkeypatch):
    from fastmcp.exceptions import ToolError

    import src.mcp_server.server as server
    from src.dependencies import Dependencies

    svc, ids = fragments
    monkeypatch.setattr(Dependencies, "get_search", lambda: svc.search_service)
    result = server.search_structure(
        FragmentSearchRequest(pattern="3.3", project_id="p", include_shared=False),
        user_id="private",
    )
    assert [h.id for h in result.hits] == [ids[5]]
    with pytest.raises(ToolError, match="another"):
        server.search_fragment_names(
            FragmentSearchRequest(
                name_query="Секрет", user_id="another", project_id="p"
            ),
            user_id="private",
        )
    with pytest.raises(ToolError, match="requires request.pattern"):
        server.search_structure(
            FragmentSearchRequest(name_query="Секрет"), user_id="private"
        )


def test_expanded_names_use_name_vectors_and_preserve_structural_filter(
    fragments, monkeypatch
):
    svc, ids = fragments

    class Embedder:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def embed_query(self, query):
            return [1.0, 0.0]

        def embed_documents(self, names):
            assert all(":" not in n for n in names)
            return [[1.0, 0.0] if "покрытие" in n else [0.0, 1.0] for n in names]

    monkeypatch.setattr(
        "src.dvd_service.services.fragment_search.create_embedder", Embedder
    )
    r = svc.search(
        FragmentSearchRequest(
            pattern="3.3",
            doc_id="doc",
            name_query="термозащитный слой",
            name_mode="expanded",
            include_children=False,
        )
    )
    assert [h.id for h in r.hits] == [ids[1]]
    assert r.hits[0].match_kind == "semantic"
