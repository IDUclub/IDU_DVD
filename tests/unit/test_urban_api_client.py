"""Unit tests for src/api_clients/urban_api_client — the Urban API territory catalogue.

Uses httpx.MockTransport (no real Urban API). Covers: the level -> document-level mapping
(including the "Город федерального значения" trap), territory parsing, name search, ancestor
path building, pagination, retry/error semantics and in-process caching.
"""

from __future__ import annotations

import httpx
import pytest

from src.api_clients import urban_api_client as uac
from src.api_clients.urban_api_client import (
    COUNTRY_TERRITORY_ID,
    LEVEL_FEDERAL,
    LEVEL_MUNICIPAL,
    LEVEL_REGIONAL,
    ScenarioNotFound,
    Territory,
    TerritoryNotFound,
    UrbanApiClient,
    UrbanApiError,
    normalized_level,
)

# Real shapes, trimmed to the fields the client reads.
RUSSIA = {
    "territory_id": 12639,
    "name": "Россия",
    "level": 1,
    "territory_type": {"id": 16, "name": "Страна"},
    "parent": None,
}
LENOBLAST = {
    "territory_id": 1,
    "name": "Ленинградская область",
    "level": 2,
    "territory_type": {"id": 1, "name": "Субъект Федерации"},
    "parent": {"id": 12639, "name": "Россия"},
}
SPB = {
    "territory_id": 3138,
    "name": "Санкт-Петербург",
    "level": 2,
    "territory_type": {"id": 17, "name": "Город федерального значения"},
    "parent": {"id": 12639, "name": "Россия"},
}
VYBORG_DISTRICT = {
    "territory_id": 54,
    "name": "Выборгский муниципальный район",
    "level": 3,
    "territory_type": {"id": 2, "name": "Муниципальное образование"},
    "parent": {"id": 1, "name": "Ленинградская область"},
}
SPB_VYBORG_DISTRICT = {
    "territory_id": 3144,
    "name": "Выборгский район",
    "level": 3,
    "territory_type": {"id": 12, "name": "Район"},
    "parent": {"id": 3138, "name": "Санкт-Петербург"},
}

BY_ID = {
    t["territory_id"]: t
    for t in (RUSSIA, LENOBLAST, SPB, VYBORG_DISTRICT, SPB_VYBORG_DISTRICT)
}


def _client_with(handler) -> UrbanApiClient:
    client = UrbanApiClient(base="http://urban-api.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _catalogue_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/territory_types":
        return httpx.Response(200, json=[{"territory_type_id": 1, "name": "Субъект"}])
    if path.startswith("/api/v1/territory/"):
        territory_id = int(path.rsplit("/", 1)[-1])
        if territory_id not in BY_ID:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json=BY_ID[territory_id])
    if path == "/api/v1/territories_without_geometry":
        parent_id = int(request.url.params.get("parent_id"))
        name = request.url.params.get("name")
        results = [
            t for t in BY_ID.values() if (t.get("parent") or {}).get("id") == parent_id
        ]
        if request.url.params.get("get_all_levels") == "true":
            results = [t for t in BY_ID.values() if t["territory_id"] != parent_id]
        if name:
            results = [t for t in results if name.lower() in t["name"].lower()]
        return httpx.Response(
            200,
            json={
                "count": len(results),
                "prev": None,
                "next": None,
                "results": results,
            },
        )
    return httpx.Response(404)


class TestNormalizedLevel:
    def test_country_is_federal(self):
        assert normalized_level(1) == LEVEL_FEDERAL

    def test_subject_is_regional(self):
        assert normalized_level(2) == LEVEL_REGIONAL

    @pytest.mark.parametrize("level", [3, 4, 5, 6])
    def test_below_subject_is_municipal(self, level):
        assert normalized_level(level) == LEVEL_MUNICIPAL

    def test_federal_city_is_regional_despite_its_type_name(self):
        """Санкт-Петербург is a *region*: the type name says "федерального", the level says 2.

        The whole reason the mapping is keyed on level and not on territory_type.
        """
        assert Territory.from_api(SPB).document_level == LEVEL_REGIONAL

    def test_city_type_is_not_a_level(self):
        """The same "Район" type appears under a region and under a federal city alike."""
        assert Territory.from_api(SPB_VYBORG_DISTRICT).document_level == LEVEL_MUNICIPAL


class TestParsing:
    def test_from_api_reads_type_and_parent(self):
        territory = Territory.from_api(VYBORG_DISTRICT)
        assert territory.territory_id == 54
        assert territory.name == "Выборгский муниципальный район"
        assert territory.level == 3
        assert territory.type_id == 2
        assert territory.type_name == "Муниципальное образование"
        assert territory.parent_id == 1
        assert territory.parent_name == "Ленинградская область"

    def test_missing_parent_is_none(self):
        assert Territory.from_api(RUSSIA).parent_id is None


class TestCatalogue:
    def test_territory_by_id(self):
        client = _client_with(_catalogue_handler)
        assert client.territory(54).name == "Выборгский муниципальный район"

    def test_unknown_territory_raises_not_found(self):
        client = _client_with(_catalogue_handler)
        with pytest.raises(TerritoryNotFound):
            client.territory(999999)

    def test_regions_are_children_of_the_country(self):
        client = _client_with(_catalogue_handler)
        names = {t.name for t in client.regions()}
        assert names == {"Ленинградская область", "Санкт-Петербург"}

    def test_find_by_name_keeps_the_parent(self):
        """Two "Выборгских" districts exist — the parent is what tells them apart."""
        client = _client_with(_catalogue_handler)
        found = client.find_by_name("Выборг")
        assert {(t.territory_id, t.parent_name) for t in found} == {
            (54, "Ленинградская область"),
            (3144, "Санкт-Петербург"),
        }

    def test_find_by_name_ignores_a_blank_query(self):
        client = _client_with(_catalogue_handler)
        assert client.find_by_name("   ") == []

    def test_ancestor_path_runs_root_first(self):
        client = _client_with(_catalogue_handler)
        assert client.ancestor_path(54) == [COUNTRY_TERRITORY_ID, 1, 54]

    def test_country_path_is_itself(self):
        client = _client_with(_catalogue_handler)
        assert client.ancestor_path(COUNTRY_TERRITORY_ID) == [COUNTRY_TERRITORY_ID]


class TestPagination:
    def test_follows_next_until_exhausted(self):
        pages = {
            1: {
                "count": 3,
                "next": "http://urban-api.test/?page=2",
                "results": [LENOBLAST],
            },
            2: {"count": 3, "next": "http://urban-api.test/?page=3", "results": [SPB]},
            3: {"count": 3, "next": None, "results": [VYBORG_DISTRICT]},
        }
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page"))
            seen.append(page)
            return httpx.Response(200, json=pages[page])

        client = _client_with(handler)
        assert len(client.children(12639)) == 3
        assert seen == [1, 2, 3]

    def test_stops_on_an_empty_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"count": 0, "next": "http://next", "results": []}
            )

        client = _client_with(handler)
        assert client.children(12639) == []


class TestErrorsAndRetries:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(uac.time, "sleep", lambda _seconds: None)

    def test_retries_a_5xx_then_succeeds(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503, text="upstream unavailable")
            return httpx.Response(200, json=VYBORG_DISTRICT)

        client = _client_with(handler)
        assert client.territory(54).territory_id == 54
        assert attempts["n"] == 3

    def test_gives_up_after_the_retry_budget(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = _client_with(handler)
        with pytest.raises(UrbanApiError):
            client.territory(54)

    def test_a_network_error_is_retried_and_then_raised(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            raise httpx.ConnectError("no route to host")

        client = _client_with(handler)
        with pytest.raises(UrbanApiError):
            client.territory(54)
        assert attempts["n"] == 3

    def test_a_4xx_is_not_retried(self):
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(400, text="bad request")

        client = _client_with(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.territory(54)
        assert attempts["n"] == 1

    def test_available_reflects_reachability(self):
        assert _client_with(_catalogue_handler).available() is True
        assert _client_with(lambda r: httpx.Response(500)).available() is False


class TestScenarioProjectLookup:
    def test_resolves_and_caches_project_id(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json={"scenario_id": 12, "project": {"project_id": 34}},
            )

        client = _client_with(handler)
        assert client.project_id_for_scenario("12", "u1") == "34"
        assert client.project_id_for_scenario(12, "u1") == "34"
        assert calls == ["/api/v1/scenarios/12"]

    def test_scenario_404_has_a_specific_error(self):
        client = _client_with(lambda _request: httpx.Response(404))
        with pytest.raises(ScenarioNotFound):
            client.project_id_for_scenario("12", "u1")

    @pytest.mark.parametrize("scenario_id", ["", "abc", "0", "-1"])
    def test_rejects_invalid_scenario_id(self, scenario_id):
        client = _client_with(lambda _request: httpx.Response(500))
        with pytest.raises(ValueError):
            client.project_id_for_scenario(scenario_id, "u1")


class TestCaching:
    def test_a_repeated_lookup_does_not_hit_the_api_twice(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return _catalogue_handler(request)

        client = _client_with(handler)
        client.territory(54)
        client.territory(54)
        assert calls == ["/api/v1/territory/54"]

    def test_the_ancestor_path_reuses_cached_territories(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return _catalogue_handler(request)

        client = _client_with(handler)
        client.ancestor_path(54)
        client.ancestor_path(54)
        assert calls == [
            "/api/v1/territory/54",
            "/api/v1/territory/1",
            "/api/v1/territory/12639",
        ]


class TestRepr:
    def test_repr_shows_the_base_url(self):
        assert "urban-api.test" in repr(UrbanApiClient(base="http://urban-api.test"))
