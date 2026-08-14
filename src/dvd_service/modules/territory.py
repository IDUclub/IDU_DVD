"""Administrative scope of a document: LLM hints -> an Urban API territory.

The head pass (:mod:`dvd_service.modules.tagging`) says, in free text, what the document is
about — "Ленинградская область", "Выборгский муниципальный район". This module turns that into
the payload slice DVD actually stores: a territory id, its ancestor chain, and the normalized
level derived from that territory's depth in the tree.

Two rules shape everything here:

* the level always follows the resolved territory (never the LLM's guess), so the pair can
  never contradict itself — a federal document is the one pointing at "Россия";
* an ambiguous match is not a match. "Кировский район" exists in a dozen regions; guessing
  would write a plausible-looking lie into the corpus, which is worse than an honest
  ``pending`` that a human resolves in the admin panel.
"""

from __future__ import annotations

import difflib
import re

import structlog

from src.api_clients import (
    COUNTRY_TERRITORY_ID,
    LEVEL_FEDERAL,
    LEVEL_MUNICIPAL,
    LEVEL_REGIONAL,
    Territory,
    UrbanApiClient,
    UrbanApiError,
)
from src.dvd_service.modules.tagging import DocumentHead

log = structlog.get_logger(__name__)

SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
SOURCE_UNSET = "unset"

STATUS_OK = "ok"
STATUS_PENDING = "pending"

# Payload keys this module owns — used by the ingestion services and the backfill job to
# carry the whole slice around without repeating the field list.
SCOPE_FIELDS = (
    "document_level",
    "territory_id",
    "territory_name",
    "territory_type_id",
    "territory_type_name",
    "territory_path",
    "level_source",
    "territory_source",
    "territory_confidence",
    "tagging_status",
    "tagging_error",
)

# Accept a match only when it is both good enough on its own and clearly better than the
# runner-up: two candidates that score alike are exactly the ambiguity we refuse to guess at.
_MIN_RATIO = 0.72
_MIN_MARGIN = 0.08

# Common contractions the Urban API never spells out. Deliberately tiny — anything longer
# belongs in the corpus, not in a hand-maintained table.
_ALIASES = {
    "ленобласть": "ленинградская область",
    "мособласть": "московская область",
    "подмосковье": "московская область",
    "спб": "санкт-петербург",
    "питер": "санкт-петербург",
    "мск": "москва",
}

_PUNCT = re.compile(r"[^\w\s]+", re.U)
_SPACES = re.compile(r"\s+", re.U)


def normalize_name(value: str) -> str:
    """Lowercase, drop punctuation and collapse spaces; expand a known contraction."""
    text = _SPACES.sub(" ", _PUNCT.sub(" ", (value or "").lower())).strip()
    return _ALIASES.get(text, text)


def _similarity(query: str, candidate: str) -> float:
    """How close two territory names are, 0..1.

    Containment and token subsets score high on purpose: the head says "Выборгский район"
    where the catalogue says "Выборгский муниципальный район", and that is a match, not a
    near-miss — plain character similarity would score it below the acceptance threshold
    because of the inserted word.
    """
    left, right = normalize_name(query), normalize_name(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.95
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if left_tokens and left_tokens <= right_tokens:
        return 0.9
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return max(difflib.SequenceMatcher(None, left, right).ratio(), overlap)


def best_match(
    query: str, candidates: list[Territory]
) -> tuple[Territory | None, float]:
    """The single best-scoring candidate, or ``(None, score)`` when the choice is not clear."""
    if not candidates:
        return None, 0.0
    scored = sorted(
        ((_similarity(query, c.name), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    top_score, top = scored[0]
    if top_score < _MIN_RATIO:
        return None, top_score
    if len(scored) > 1 and top_score - scored[1][0] < _MIN_MARGIN:
        return None, top_score  # a tie is an ambiguity, not a winner
    return top, top_score


def untagged_scope() -> dict:
    """The payload slice of a document whose scope is not known (yet)."""
    return {
        "document_level": None,
        "territory_id": None,
        "territory_name": None,
        "territory_type_id": None,
        "territory_type_name": None,
        "territory_path": [],
        "level_source": SOURCE_UNSET,
        "territory_source": SOURCE_UNSET,
        "territory_confidence": None,
        "tagging_status": STATUS_PENDING,
        "tagging_error": None,
    }


def pending_scope(reason: str) -> dict:
    """Nothing resolved, and the reason recorded for the admin panel."""
    return {**untagged_scope(), "tagging_error": reason}


def manual_scope_fields(payload: dict) -> dict:
    """The manually set part of an existing payload, or ``{}``.

    Automatic detection must never overwrite a human's decision — not on re-upload, not on a
    new version, not on a full reload — so every write path asks this first and keeps whatever
    it returns.
    """
    if (payload or {}).get("territory_source") != SOURCE_MANUAL:
        return {}
    return {field: payload.get(field) for field in SCOPE_FIELDS if field in payload}


class TerritoryResolver:
    """Resolves a document's administrative scope against the Urban API territory tree."""

    def __init__(self, urban: UrbanApiClient) -> None:
        self.urban = urban

    def __repr__(self) -> str:
        return f"{type(self).__name__}(urban={self.urban!r})"

    # --- building the payload slice -----------------------------------------------------

    def _scope_of(
        self, territory: Territory, source: str, confidence: float | None
    ) -> dict:
        return {
            "document_level": territory.document_level,
            "territory_id": territory.territory_id,
            "territory_name": territory.name,
            "territory_type_id": territory.type_id,
            "territory_type_name": territory.type_name,
            "territory_path": self.urban.ancestor_path(territory.territory_id),
            # The level is derived from the territory, so it shares its provenance.
            "level_source": source,
            "territory_source": source,
            "territory_confidence": confidence,
            "tagging_status": STATUS_OK,
            "tagging_error": None,
        }

    def by_territory_id(
        self, territory_id: int, *, source: str = SOURCE_MANUAL
    ) -> dict:
        """Scope for an explicitly chosen territory — the admin panel and the upload forms.

        Raises :class:`UrbanApiError` (or ``TerritoryNotFound``) so the caller can answer the
        request with a real error instead of silently storing an unresolved id.
        """
        territory = self.urban.territory(int(territory_id))
        return self._scope_of(territory, source, 1.0)

    # --- automatic detection ------------------------------------------------------------

    def _match_region(self, name: str) -> tuple[Territory | None, float]:
        return best_match(name, self.urban.regions())

    def _match_municipality(
        self, name: str, region_hint: str
    ) -> tuple[Territory | None, float, str]:
        """A municipality by name, disambiguated by its region when the head named one.

        The Urban API filters by name server-side, so this is one request over the whole tree
        rather than a walk across 89 regions.
        """
        candidates = [
            candidate
            for candidate in self.urban.find_by_name(name, limit=0)
            if candidate.level >= 3
        ]
        if not candidates:
            return None, 0.0, "территория не найдена в Urban API"
        if len(candidates) > 1 and region_hint:
            region, _score = self._match_region(region_hint)
            if region is not None:
                narrowed = [
                    candidate
                    for candidate in candidates
                    if region.territory_id
                    in self.urban.ancestor_path(candidate.territory_id)
                ]
                if narrowed:
                    candidates = narrowed
        match, score = best_match(name, candidates)
        if match is None:
            return None, score, "неоднозначное название территории"
        return match, score, ""

    def from_hints(self, head: DocumentHead) -> dict:
        """Resolve the head hints into a scope, or return a ``pending`` slice with the reason.

        Never raises: an Urban API outage leaves the document indexed and untagged, for the
        backfill job to finish later.
        """
        level_hint = (head.level_hint or "unknown").lower()
        try:
            if level_hint == LEVEL_FEDERAL:
                country = self.urban.territory(COUNTRY_TERRITORY_ID)
                return self._scope_of(country, SOURCE_AUTO, 1.0)

            if level_hint not in (LEVEL_REGIONAL, LEVEL_MUNICIPAL):
                return pending_scope("уровень документа не определён")
            if not head.territory_hint:
                return pending_scope("территория не названа в документе")

            if level_hint == LEVEL_REGIONAL:
                match, score = self._match_region(head.territory_hint)
                if match is None:
                    return pending_scope("субъект РФ не распознан")
            else:
                match, score, reason = self._match_municipality(
                    head.territory_hint, head.region_hint
                )
                if match is None:
                    return pending_scope(reason)
            return self._scope_of(match, SOURCE_AUTO, round(score, 3))
        except UrbanApiError as exc:
            log.warning("territory_resolve_unavailable", error=str(exc))
            return pending_scope(f"Urban API недоступен: {exc}")
        except Exception as exc:  # noqa: BLE001 — tagging must never fail an ingest
            log.warning("territory_resolve_failed", error=str(exc))
            return pending_scope(f"ошибка определения территории: {exc}")
