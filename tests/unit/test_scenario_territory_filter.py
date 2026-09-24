"""A scenario narrows the shared corpus to the territories under its project boundary.

Covers the Urban API calls (scenario -> project/region, POST intersecting_territories), the
tree descent and its fallbacks in ``TerritoryResolver.scenario_scope``, and the resulting
filter evaluated by a real in-memory Qdrant: what passes for «город Светогорск» (the dev
stand's scenario 772) and what the overrides switch off.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from qdrant_client import QdrantClient, models

from src.api_clients import (
    ScenarioNotFound,
    ScenarioProject,
    Territory,
    UrbanApiClient,
    UrbanApiError,
)
from src.common.config import settings as global_settings
from src.common.db.qdrant_client import QdrantRepository
from src.dvd_service.dto import SearchRequest
from src.dvd_service.dto.fragment_search import FragmentSearchRequest
from src.dvd_service.modules.territory import (
    SCENARIO_SOURCE_GEOMETRY,
    SCENARIO_SOURCE_REGION,
    TerritoryResolver,
)
from src.dvd_service.services.dvd_service import (
    LibraryService,
    SearchService,
    TagsService,
    scenario_listing_condition,
)
from src.dvd_service.services.fragment_search import FragmentSearchService

RUSSIA, LENOBLAST, MOSCOW = 12639, 1, 2
VYBORG_DISTRICT, SVETOGORSK_GP, SVETOGORSK, PRIMORSK_GP = 54, 58, 1955, 70
PARENTS = {
    LENOBLAST: RUSSIA,
    MOSCOW: RUSSIA,
    VYBORG_DISTRICT: LENOBLAST,
    SVETOGORSK_GP: VYBORG_DISTRICT,
    SVETOGORSK: SVETOGORSK_GP,
    PRIMORSK_GP: VYBORG_DISTRICT,
}
BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[28.8, 61.1], [28.9, 61.1], [28.8, 61.1]]],
}


def _territory(territory_id: int, level: int) -> Territory:
    return Territory(territory_id=territory_id, name=str(territory_id), level=level)


class FakeUrbanApi:
    """Urban API as the dev stand answers for scenario 772 (project 604)."""

    def __init__(self, *, intersections=None, geometry=BOUNDARY, regional=False):
        self.intersections = (
            {
                LENOBLAST: [_territory(VYBORG_DISTRICT, 3)],
                VYBORG_DISTRICT: [_territory(SVETOGORSK_GP, 4)],
                SVETOGORSK_GP: [_territory(SVETOGORSK, 5)],
            }
            if intersections is None
            else intersections
        )
        self.geometry = geometry
        self.regional = regional
        self.calls: list[str] = []
        self.broken_at: str | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.broken_at and name.startswith(self.broken_at):
            raise UrbanApiError("connection refused")

    def scenario_project(self, scenario_id, user_id) -> ScenarioProject:
        self._call(f"scenario:{scenario_id}")
        if str(scenario_id) == "404":
            raise ScenarioNotFound("Urban API 404: /v1/scenarios/404")
        return ScenarioProject(project_id="604", region_id=LENOBLAST)

    def project_id_for_scenario(self, scenario_id, user_id) -> str:
        return self.scenario_project(scenario_id, user_id).project_id

    def project(self, project_id, user_id) -> dict:
        self._call(f"project:{project_id}")
        return {"project_id": int(project_id), "is_regional": self.regional}

    def project_geometry(self, project_id, user_id):
        self._call(f"geometry:{project_id}")
        return self.geometry

    def intersecting_territories(self, parent_id, geometry):
        self._call(f"intersect:{parent_id}")
        assert geometry == self.geometry
        return self.intersections.get(parent_id, [])

    def ancestor_path(self, territory_id: int) -> list[int]:
        path, current = [], territory_id
        while current is not None:
            path.append(current)
            current = PARENTS.get(current)
        return path[::-1]


@pytest.fixture(autouse=True)
def scenario_settings(monkeypatch):
    monkeypatch.setattr(global_settings, "scenario_territory_filter", True)
    monkeypatch.setattr(global_settings, "scenario_territory_cache_ttl", 3600.0)
    monkeypatch.setattr(global_settings, "scenario_territory_max_requests", 100)
    return global_settings


# ── Urban API client ─────────────────────────────────────────────────────────


def _client_with(handler) -> UrbanApiClient:
    client = UrbanApiClient(base="http://urban-api.test/api")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_scenario_lookup_also_reports_the_projects_region():
    client = _client_with(
        lambda request: httpx.Response(
            200,
            json={
                "scenario_id": 772,
                "project": {"project_id": 604, "region": {"id": 1, "name": "ЛО"}},
            },
        )
    )
    assert client.scenario_project(772, "u1") == ScenarioProject("604", 1)
    assert client.project_id_for_scenario("772", "u1") == "604"


def test_intersecting_territories_posts_the_boundary():
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json=[
                {
                    "territory_id": 54,
                    "name": "Выборгский муниципальный район",
                    "level": 3,
                    "territory_type": {"id": 4, "name": "Муниципальное образование"},
                    "parent": {"id": 1, "name": "Ленинградская область"},
                }
            ],
        )

    found = _client_with(handler).intersecting_territories(1, BOUNDARY)

    assert [(t.territory_id, t.level, t.parent_id) for t in found] == [(54, 3, 1)]
    assert seen == [("POST", "/api/v1/territory/1/intersecting_territories", BOUNDARY)]


def test_a_project_without_a_boundary_has_no_geometry():
    client = _client_with(lambda _request: httpx.Response(404))
    assert client.project_geometry(604, "u1") is None


# ── resolving the scenario scope ─────────────────────────────────────────────


def test_descends_to_the_deepest_territory_under_the_boundary():
    urban = FakeUrbanApi()
    scope = TerritoryResolver(urban).scenario_scope("772", "u1")

    assert scope.territory_ids == (SVETOGORSK,)
    assert scope.ancestor_ids == (
        LENOBLAST,
        VYBORG_DISTRICT,
        SVETOGORSK_GP,
        SVETOGORSK,
        RUSSIA,
    )
    assert scope.source == SCENARIO_SOURCE_GEOMETRY
    assert [c for c in urban.calls if c.startswith("intersect")] == [
        "intersect:1",
        "intersect:54",
        "intersect:58",
        "intersect:1955",
    ]


def test_every_branch_keeps_its_own_deepest_territory():
    urban = FakeUrbanApi(
        intersections={
            LENOBLAST: [_territory(VYBORG_DISTRICT, 3), _territory(77, 3)],
            VYBORG_DISTRICT: [_territory(SVETOGORSK_GP, 4)],
        }
    )
    scope = TerritoryResolver(urban).scenario_scope("772", "u1")
    assert scope.territory_ids == (SVETOGORSK_GP, 77)


def test_a_spent_request_budget_stops_at_the_current_level(scenario_settings):
    scenario_settings.scenario_territory_max_requests = 2
    scope = TerritoryResolver(FakeUrbanApi()).scenario_scope("772", "u1")
    assert scope.territory_ids == (SVETOGORSK_GP,)


def test_a_regional_project_covers_its_region():
    urban = FakeUrbanApi(regional=True)
    scope = TerritoryResolver(urban).scenario_scope("772", "u1")

    assert scope.territory_ids == (LENOBLAST,)
    assert scope.source == SCENARIO_SOURCE_REGION
    assert not [c for c in urban.calls if c.startswith(("geometry", "intersect"))]


@pytest.mark.parametrize("broken_at", ["geometry", "intersect:54"])
def test_an_outage_or_a_missing_boundary_falls_back_to_the_region(broken_at):
    urban = FakeUrbanApi()
    urban.broken_at = broken_at
    scope = TerritoryResolver(urban).scenario_scope("772", "u1")
    assert (scope.territory_ids, scope.source) == ((LENOBLAST,), SCENARIO_SOURCE_REGION)

    no_boundary = TerritoryResolver(FakeUrbanApi(geometry=None))
    assert no_boundary.scenario_scope("772", "u1").territory_ids == (LENOBLAST,)


def test_a_scenario_lookup_outage_leaves_the_corpus_unfiltered():
    urban = FakeUrbanApi()
    urban.broken_at = "scenario"
    assert TerritoryResolver(urban).scenario_scope("772", "u1") is None


def test_an_unknown_scenario_is_an_error_not_an_outage():
    with pytest.raises(ScenarioNotFound):
        TerritoryResolver(FakeUrbanApi()).scenario_scope("404", "u1")


def test_the_scope_is_cached_and_a_degraded_one_only_briefly(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("src.dvd_service.modules.territory.time.time", lambda: clock[0])
    urban = FakeUrbanApi()
    resolver = TerritoryResolver(urban)

    resolver.scenario_scope("772", "u1")
    calls = len(urban.calls)
    clock[0] += 3000
    resolver.scenario_scope("772", "u1")
    assert len(urban.calls) == calls

    urban.broken_at = "geometry"
    resolver.scenario_scope("773", "u1")
    urban.broken_at = None
    clock[0] += 61
    assert resolver.scenario_scope("773", "u1").territory_ids == (SVETOGORSK,)


def test_the_setting_switches_the_filter_off(scenario_settings):
    scenario_settings.scenario_territory_filter = False
    urban = FakeUrbanApi()
    assert TerritoryResolver(urban).scenario_scope("772", "u1") is None
    assert urban.calls == []


# ── the filter over a real (in-memory) Qdrant ────────────────────────────────


DOCUMENTS = {
    "federal": {"territory_id": RUSSIA, "territory_path": [RUSSIA]},
    "lenoblast": {"territory_id": LENOBLAST, "territory_path": [RUSSIA, LENOBLAST]},
    "vyborg": {
        "territory_id": VYBORG_DISTRICT,
        "territory_path": [RUSSIA, LENOBLAST, VYBORG_DISTRICT],
    },
    "svetogorsk": {
        "territory_id": SVETOGORSK,
        "territory_path": [
            RUSSIA,
            LENOBLAST,
            VYBORG_DISTRICT,
            SVETOGORSK_GP,
            SVETOGORSK,
        ],
    },
    "primorsk": {
        "territory_id": PRIMORSK_GP,
        "territory_path": [RUSSIA, LENOBLAST, VYBORG_DISTRICT, PRIMORSK_GP],
    },
    "moscow": {"territory_id": MOSCOW, "territory_path": [RUSSIA, MOSCOW]},
    "untagged": {"tagging_status": "pending"},
    "project_note": {
        "user_id": "u1",
        "project_id": "604",
        "territory_id": MOSCOW,
        "territory_path": [RUSSIA, MOSCOW],
    },
    "foreign_project": {"user_id": "u2", "project_id": "999"},
}
IN_FORCE_IN_SVETOGORSK = {
    "federal",
    "lenoblast",
    "vyborg",
    "svetogorsk",
    "untagged",
    "project_note",
}


@pytest.fixture
def corpus(settings):
    repo = QdrantRepository.__new__(QdrantRepository)
    repo.settings = settings
    repo.collection = "fragments"
    repo.client = QdrantClient(":memory:")
    repo.client.create_collection(
        repo.collection,
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
    )
    for order, (name, scope) in enumerate(DOCUMENTS.items()):
        payload = {
            "doc_id": name,
            "name": name,
            "version": "1",
            "versions": ["1"],
            "kind": "text",
            "type": "clause",
            "numbering": "1",
            "text": name,
            "order": order,
            **scope,
        }
        repo.client.upsert(
            repo.collection,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()), vector=[1.0, 0.0], payload=payload
                )
            ],
        )
    urban = FakeUrbanApi()
    resolver = TerritoryResolver(urban)
    search = SearchService(repo, settings, None, territory=resolver, urban_api=urban)
    yield repo, search, resolver
    repo.client.close()


def _names(repo, query_filter) -> set[str]:
    points, _ = repo.client.scroll(
        repo.collection, scroll_filter=query_filter, limit=100, with_payload=True
    )
    return {p.payload["name"] for p in points}


def _request(**overrides) -> SearchRequest:
    return SearchRequest(query="q", user_id="u1", scenario_id="772", **overrides)


def test_a_scenario_search_sees_what_is_in_force_there(corpus):
    repo, search, _ = corpus
    assert (
        _names(repo, search._build_filter(_request(), None)) == IN_FORCE_IN_SVETOGORSK
    )


UNFILTERED = set(DOCUMENTS) - {"foreign_project"}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"scenario_territory_filter": False}, UNFILTERED),
        ({"document_names": ["moscow", "federal"]}, {"moscow", "federal"}),
        ({"name": "moscow"}, {"moscow"}),
        ({"doc_id": "moscow"}, {"moscow"}),
    ],
)
def test_a_named_document_or_the_switch_lifts_the_scenario_filter(
    corpus, overrides, expected
):
    repo, search, _ = corpus
    assert _names(repo, search._build_filter(_request(**overrides), None)) == expected


def test_explicit_territories_replace_the_scenario(corpus):
    repo, search, _ = corpus
    found = _names(repo, search._build_filter(_request(territory_ids=[MOSCOW]), None))
    assert found == {"federal", "moscow", "project_note"}


def test_a_project_only_search_is_not_narrowed(corpus):
    repo, search, _ = corpus
    found = _names(repo, search._build_filter(_request(include_shared=False), None))
    assert found == {"project_note"}


def test_structural_search_keeps_a_named_document_and_filters_the_rest(corpus):
    repo, search, _ = corpus
    service = FragmentSearchService(search)
    base = {"user_id": "u1", "scenario_id": "772", "pattern": "1"}

    anywhere = service.search(FragmentSearchRequest(**base))
    named = service.search(FragmentSearchRequest(**base, document_names=["moscow"]))

    assert {hit.name for hit in anywhere.hits} == IN_FORCE_IN_SVETOGORSK
    assert {hit.name for hit in named.hits} == {"moscow"}


def test_listings_and_scopes_are_narrowed_to_the_scenario(corpus):
    repo, _, resolver = corpus
    condition = scenario_listing_condition(resolver, "772", "u1")
    shared_in_force = IN_FORCE_IN_SVETOGORSK - {"project_note"}

    listed = LibraryService(repo, None).list_documents(scenario_condition=condition)
    scopes = TagsService(repo).get_scopes(condition)

    assert {d.name for d in listed.documents} == shared_in_force
    assert {t.territory_id for t in scopes.territories} == {
        RUSSIA,
        LENOBLAST,
        VYBORG_DISTRICT,
        SVETOGORSK,
    }
    assert scenario_listing_condition(resolver, "772", "u1", territory_ids=[2]) is None
    assert scenario_listing_condition(resolver, "772", "u1", enabled=False) is None
    assert scenario_listing_condition(resolver, None, None) is None
    with pytest.raises(ValueError):
        scenario_listing_condition(resolver, "772", None)
