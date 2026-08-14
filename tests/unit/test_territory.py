"""Unit tests for src/dvd_service/modules/territory — TerritoryResolver.

The Urban API is faked, so these cover the resolution logic itself: name normalization and
matching, the federal/regional/municipal branches, disambiguation by region, and the two
invariants that matter most — an ambiguous match is never guessed at, and an Urban API outage
degrades to ``pending`` instead of raising.
"""

from __future__ import annotations

import pytest

from src.api_clients import COUNTRY_TERRITORY_ID, Territory, UrbanApiError
from src.dvd_service.modules.tagging import DocumentHead
from src.dvd_service.modules.territory import (
    SOURCE_AUTO,
    SOURCE_MANUAL,
    SOURCE_UNSET,
    STATUS_OK,
    STATUS_PENDING,
    TerritoryResolver,
    best_match,
    manual_scope_fields,
    normalize_name,
    pending_scope,
    untagged_scope,
)

RUSSIA = Territory(COUNTRY_TERRITORY_ID, "Россия", 1, 16, "Страна", None, None)
LENOBLAST = Territory(
    1, "Ленинградская область", 2, 1, "Субъект Федерации", 12639, "Россия"
)
SPB = Territory(
    3138, "Санкт-Петербург", 2, 17, "Город федерального значения", 12639, "Россия"
)
VYBORG = Territory(
    54,
    "Выборгский муниципальный район",
    3,
    2,
    "Муниципальное образование",
    1,
    "Ленинградская область",
)
SPB_VYBORG = Territory(
    3144, "Выборгский район", 3, 12, "Район", 3138, "Санкт-Петербург"
)
KIROV_LO = Territory(70, "Кировский район", 3, 12, "Район", 1, "Ленинградская область")
KIROV_SPB = Territory(3150, "Кировский район", 3, 12, "Район", 3138, "Санкт-Петербург")

ALL = [RUSSIA, LENOBLAST, SPB, VYBORG, SPB_VYBORG, KIROV_LO, KIROV_SPB]
PARENTS = {t.territory_id: t.parent_id for t in ALL}


class FakeUrbanApi:
    """In-memory stand-in for UrbanApiClient, optionally broken."""

    def __init__(self, territories=ALL, broken: bool = False) -> None:
        self.by_id = {t.territory_id: t for t in territories}
        self.broken = broken
        self.calls: list[str] = []

    def _guard(self) -> None:
        if self.broken:
            raise UrbanApiError("connection refused")

    def territory(self, territory_id: int) -> Territory:
        self.calls.append(f"territory:{territory_id}")
        self._guard()
        return self.by_id[int(territory_id)]

    def regions(self) -> list[Territory]:
        self.calls.append("regions")
        self._guard()
        return [t for t in self.by_id.values() if t.level == 2]

    def find_by_name(self, query: str, limit: int = 20, **_kwargs) -> list[Territory]:
        self.calls.append(f"find:{query}")
        self._guard()
        found = [t for t in self.by_id.values() if query.lower() in t.name.lower()]
        return found[:limit] if limit else found

    def ancestor_path(self, territory_id: int) -> list[int]:
        self._guard()
        path: list[int] = []
        current = int(territory_id)
        while current is not None:
            path.append(current)
            current = PARENTS.get(current)
        path.reverse()
        return path


@pytest.fixture
def resolver() -> TerritoryResolver:
    return TerritoryResolver(FakeUrbanApi())


class TestNormalization:
    def test_case_and_punctuation_are_ignored(self):
        assert normalize_name("  Ленинградская  ОБЛАСТЬ. ") == "ленинградская область"

    def test_known_contraction_is_expanded(self):
        assert normalize_name("Ленобласть") == "ленинградская область"
        assert normalize_name("СПб") == "санкт-петербург"


class TestBestMatch:
    def test_exact_name_wins(self):
        match, score = best_match("Ленинградская область", [LENOBLAST, SPB])
        assert match is LENOBLAST and score == 1.0

    def test_containment_counts_as_a_match(self):
        """The head says "Выборгский район", the catalogue says "...муниципальный район"."""
        match, _score = best_match("Выборгский район", [VYBORG, SPB])
        assert match is VYBORG

    def test_a_tie_is_not_a_match(self):
        """Two identically named districts: refusing to choose is the point."""
        match, _score = best_match("Кировский район", [KIROV_LO, KIROV_SPB])
        assert match is None

    def test_nothing_close_enough(self):
        match, score = best_match("Республика Татарстан", [LENOBLAST, SPB])
        assert match is None and score < 0.72

    def test_empty_candidates(self):
        assert best_match("что угодно", []) == (None, 0.0)


class TestFederal:
    def test_federal_points_at_russia(self, resolver):
        scope = resolver.from_hints(DocumentHead("СП 1", "СП 1", "federal"))
        assert scope["territory_id"] == COUNTRY_TERRITORY_ID
        assert scope["document_level"] == "federal"
        assert scope["territory_path"] == [COUNTRY_TERRITORY_ID]
        assert scope["tagging_status"] == STATUS_OK
        assert scope["territory_source"] == SOURCE_AUTO


class TestRegional:
    def test_region_is_matched_by_name(self, resolver):
        scope = resolver.from_hints(
            DocumentHead("n", "v", "regional", "Ленинградская область")
        )
        assert scope["territory_id"] == 1
        assert scope["document_level"] == "regional"
        assert scope["territory_path"] == [COUNTRY_TERRITORY_ID, 1]

    def test_federal_city_is_regional(self, resolver):
        """The level comes from the tree, not from the type name."""
        scope = resolver.from_hints(
            DocumentHead("n", "v", "regional", "Санкт-Петербург")
        )
        assert scope["territory_id"] == 3138 and scope["document_level"] == "regional"

    def test_unknown_region_stays_pending(self, resolver):
        scope = resolver.from_hints(DocumentHead("n", "v", "regional", "Атлантида"))
        assert scope["tagging_status"] == STATUS_PENDING
        assert scope["territory_id"] is None
        assert "не распознан" in scope["tagging_error"]


class TestMunicipal:
    def test_unique_municipality_is_matched(self, resolver):
        scope = resolver.from_hints(
            DocumentHead("n", "v", "municipal", "Выборгский муниципальный район")
        )
        assert scope["territory_id"] == 54
        assert scope["document_level"] == "municipal"
        assert scope["territory_type_name"] == "Муниципальное образование"
        assert scope["territory_path"] == [COUNTRY_TERRITORY_ID, 1, 54]

    def test_region_hint_disambiguates_a_shared_name(self, resolver):
        scope = resolver.from_hints(
            DocumentHead(
                "n", "v", "municipal", "Кировский район", "Ленинградская область"
            )
        )
        assert scope["territory_id"] == 70
        assert scope["tagging_status"] == STATUS_OK

    def test_ambiguity_without_a_region_hint_stays_pending(self, resolver):
        """The case the whole design refuses to guess at."""
        scope = resolver.from_hints(
            DocumentHead("n", "v", "municipal", "Кировский район")
        )
        assert scope["tagging_status"] == STATUS_PENDING
        assert scope["territory_id"] is None
        assert "неоднозначное" in scope["tagging_error"]

    def test_unknown_municipality_stays_pending(self, resolver):
        scope = resolver.from_hints(DocumentHead("n", "v", "municipal", "Нью-Йорк"))
        assert scope["tagging_status"] == STATUS_PENDING
        assert "не найдена" in scope["tagging_error"]


class TestInconclusiveHints:
    def test_unknown_level_stays_pending(self, resolver):
        scope = resolver.from_hints(
            DocumentHead("n", "v", "unknown", "Ленинградская область")
        )
        assert scope["tagging_status"] == STATUS_PENDING
        assert "уровень" in scope["tagging_error"]

    def test_named_level_without_a_territory_stays_pending(self, resolver):
        scope = resolver.from_hints(DocumentHead("n", "v", "regional", ""))
        assert scope["tagging_status"] == STATUS_PENDING
        assert "не названа" in scope["tagging_error"]


class TestUrbanApiOutage:
    def test_an_outage_degrades_to_pending_and_never_raises(self):
        """The scenario the whole pending/backfill machinery exists for."""
        resolver = TerritoryResolver(FakeUrbanApi(broken=True))
        scope = resolver.from_hints(
            DocumentHead("n", "v", "regional", "Ленинградская область")
        )
        assert scope["tagging_status"] == STATUS_PENDING
        assert scope["territory_id"] is None
        assert "Urban API недоступен" in scope["tagging_error"]

    def test_a_manual_lookup_still_raises(self):
        """An explicit choice must fail loudly instead of storing an unresolved id."""
        resolver = TerritoryResolver(FakeUrbanApi(broken=True))
        with pytest.raises(UrbanApiError):
            resolver.by_territory_id(54)


class TestManualScope:
    def test_by_territory_id_is_marked_manual(self, resolver):
        scope = resolver.by_territory_id(54)
        assert scope["territory_source"] == SOURCE_MANUAL
        assert scope["level_source"] == SOURCE_MANUAL
        assert scope["document_level"] == "municipal"
        assert scope["territory_confidence"] == 1.0
        assert scope["tagging_status"] == STATUS_OK

    def test_manual_fields_are_extracted_from_a_payload(self):
        payload = {
            "territory_source": SOURCE_MANUAL,
            "level_source": SOURCE_MANUAL,
            "document_level": "regional",
            "territory_id": 1,
            "territory_name": "Ленинградская область",
            "territory_type_id": 1,
            "territory_type_name": "Субъект Федерации",
            "territory_path": [12639, 1],
            "territory_confidence": 1.0,
            "tagging_status": STATUS_OK,
            "tagging_error": None,
        }
        assert manual_scope_fields(payload)["territory_id"] == 1

    def test_automatic_fields_are_not_inherited(self):
        assert (
            manual_scope_fields({"territory_source": SOURCE_AUTO, "territory_id": 1})
            == {}
        )

    def test_untagged_payload_yields_nothing(self):
        assert manual_scope_fields({}) == {}


class TestScopeSlices:
    def test_untagged_scope_is_pending_and_unset(self):
        scope = untagged_scope()
        assert scope["tagging_status"] == STATUS_PENDING
        assert scope["territory_source"] == SOURCE_UNSET
        assert scope["territory_path"] == []

    def test_pending_scope_records_the_reason(self):
        assert pending_scope("почему-то")["tagging_error"] == "почему-то"
