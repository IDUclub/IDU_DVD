"""Integration tests for the Urban API territory catalogue (skipped when the stand is down).

These pin the assumptions the tagging pipeline makes about a service DVD does not own: that
the catalogue is reachable without a token, that "Россия" is the root, that the 89 subjects
sit one level below it, and that a name search works server-side across the whole tree.
"""

from __future__ import annotations

import pytest

from src.api_clients import COUNTRY_TERRITORY_ID, LEVEL_MUNICIPAL, LEVEL_REGIONAL
from src.dvd_service.modules.tagging import DocumentHead
from src.dvd_service.modules.territory import TerritoryResolver

pytestmark = pytest.mark.integration


class TestCatalogue:
    def test_country_is_the_root_of_the_tree(self, require_urban_api):
        russia = require_urban_api.territory(COUNTRY_TERRITORY_ID)
        assert russia.name == "Россия"
        assert russia.level == 1 and russia.parent_id is None

    def test_regions_sit_one_level_below_the_country(self, require_urban_api):
        regions = require_urban_api.regions()
        assert len(regions) > 50, "the catalogue should hold the subjects of the federation"
        assert all(region.level == 2 for region in regions)

    def test_name_search_spans_the_whole_tree(self, require_urban_api):
        found = require_urban_api.find_by_name("Выборг")
        assert found, "server-side name search backs both tagging and the admin autocomplete"
        assert any(t.parent_name for t in found), "parents disambiguate identical names"

    def test_ancestor_path_starts_at_the_country(self, require_urban_api):
        region = require_urban_api.regions()[0]
        path = require_urban_api.ancestor_path(region.territory_id)
        assert path[0] == COUNTRY_TERRITORY_ID
        assert path[-1] == region.territory_id


class TestResolutionAgainstTheRealCatalogue:
    def test_a_federal_document_resolves_to_russia(self, require_urban_api):
        resolver = TerritoryResolver(require_urban_api)
        scope = resolver.from_hints(DocumentHead("СП 42.13330", "СП 42.13330", "federal"))
        assert scope["territory_id"] == COUNTRY_TERRITORY_ID
        assert scope["tagging_status"] == "ok"

    def test_a_region_resolves_by_name(self, require_urban_api):
        resolver = TerritoryResolver(require_urban_api)
        scope = resolver.from_hints(
            DocumentHead("n", "v", "regional", "Ленинградская область")
        )
        assert scope["document_level"] == LEVEL_REGIONAL
        assert scope["territory_name"] == "Ленинградская область"

    def test_a_federal_city_is_regional_not_federal(self, require_urban_api):
        """The trap the level mapping exists for, checked against real data."""
        resolver = TerritoryResolver(require_urban_api)
        scope = resolver.from_hints(DocumentHead("n", "v", "regional", "Санкт-Петербург"))
        assert scope["document_level"] == LEVEL_REGIONAL
        assert "федерального значения" in (scope["territory_type_name"] or "")

    def test_a_municipality_resolves_below_its_region(self, require_urban_api):
        resolver = TerritoryResolver(require_urban_api)
        scope = resolver.from_hints(
            DocumentHead(
                "n", "v", "municipal", "Выборгский район", "Ленинградская область"
            )
        )
        assert scope["document_level"] == LEVEL_MUNICIPAL
        assert scope["territory_path"][0] == COUNTRY_TERRITORY_ID
        assert len(scope["territory_path"]) >= 3
