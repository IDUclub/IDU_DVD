"""Urban API client — the territory tree behind document level/territory tagging.

Only the public, unauthenticated part of the Urban API is used (``/api/v1/territory_types``,
``/api/v1/territories_without_geometry``, ``/api/v1/territory/{id}``), so no JWT is passed:
DVD tags documents with an administrative scope, it does not read anyone's project data.

Synchronous, like :class:`OllamaClient` and the embeddings clients — ingestion runs in a
threadpool. Lookups are memoized in-process with a long TTL: the territory tree changes on a
scale of months, one process serves one ingest queue (the same single-instance assumption the
startup job sweep already makes), and this keeps the client free of a Redis dependency that no
other API client has.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
import structlog

from src.common.config import settings

log = structlog.get_logger(__name__)

# "Россия" — the root of the territory tree (level 1). Federal documents are tagged with it,
# so a document never carries a level without a territory, and every other territory's
# ancestor path starts here (a federal document therefore matches any territory filter).
COUNTRY_TERRITORY_ID = 12639

LEVEL_FEDERAL = "federal"
LEVEL_REGIONAL = "regional"
LEVEL_MUNICIPAL = "municipal"
DOCUMENT_LEVELS = (LEVEL_FEDERAL, LEVEL_REGIONAL, LEVEL_MUNICIPAL)

# A transient 5xx / connection reset from the Urban API clears up on its own; a 4xx means the
# request itself is wrong and is never retried. Same convention as the embeddings client.
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0

_CACHE_TTL_SECONDS = 24 * 3600
_PAGE_SIZE = 500
_MAX_PAGES = 400  # hard stop: a paginated walk must never spin forever
_MAX_ANCESTOR_DEPTH = 12  # the real tree is 5-6 deep; this only guards against a cycle


def normalized_level(tree_level: int) -> str:
    """Map an Urban API tree ``level`` onto the document-level enum.

    1 ("Россия") -> federal, 2 (subject of the federation) -> regional, deeper -> municipal.

    Deliberately keyed on the tree level and not on ``territory_type``: a type does not
    determine depth ("Город" is a level-3 municipality in one place and a settlement inside
    one in another), and "Город федерального значения" (Москва, Санкт-Петербург) is a
    *region* despite what its name suggests.
    """
    if tree_level <= 1:
        return LEVEL_FEDERAL
    if tree_level == 2:
        return LEVEL_REGIONAL
    return LEVEL_MUNICIPAL


class UrbanApiError(RuntimeError):
    """Urban API is unreachable or answered with an error."""


class TerritoryNotFound(UrbanApiError):
    """The requested territory id does not exist in the Urban API."""


@dataclass(frozen=True)
class Territory:
    """One node of the Urban API territory tree, reduced to what DVD stores."""

    territory_id: int
    name: str
    level: int
    type_id: int | None = None
    type_name: str | None = None
    parent_id: int | None = None
    parent_name: str | None = None

    @property
    def document_level(self) -> str:
        return normalized_level(self.level)

    @classmethod
    def from_api(cls, data: dict) -> "Territory":
        territory_type = data.get("territory_type") or {}
        parent = data.get("parent") or {}
        return cls(
            territory_id=int(data["territory_id"]),
            name=(data.get("name") or "").strip(),
            level=int(data.get("level") or 0),
            type_id=territory_type.get("id"),
            type_name=territory_type.get("name"),
            parent_id=parent.get("id"),
            parent_name=parent.get("name"),
        )


class _TTLCache:
    """Tiny thread-safe TTL cache — ingestion and the backfill job share one client."""

    def __init__(self, ttl: float = _CACHE_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
        if not entry:
            return None
        stored_at, value = entry
        if time.time() - stored_at > self.ttl:
            with self._lock:
                self._data.pop(key, None)
            return None
        return value

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class UrbanApiClient:
    """Synchronous client for the Urban API territory catalogue."""

    def __init__(self, base: str | None = None, timeout: float | None = None) -> None:
        self.base = (base or settings.urban_api_url).rstrip("/")
        self.timeout = timeout or settings.urban_api_timeout
        self._client = httpx.Client(timeout=self.timeout)
        self._cache = _TTLCache()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base={self.base})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UrbanApiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def available(self) -> bool:
        """Cheap reachability probe — used by the health endpoint and the backfill job."""
        try:
            self._client.get(
                self.base + "/api/v1/territory_types", timeout=5
            ).raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("urban_api_unavailable", error=str(exc))
            return False

    # --- transport ---------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None):
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.get(self.base + path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 404:
                    raise TerritoryNotFound(f"Urban API 404: {path}")
                if response.status_code < 500:
                    response.raise_for_status()
                    return response.json()
                last_error = UrbanApiError(
                    f"Urban API {response.status_code}: {response.text[:200]}"
                )
            if attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_BASE_SECONDS * (2**attempt)
                log.warning(
                    "urban_api_retry",
                    path=path,
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(last_error),
                )
                time.sleep(delay)
        raise UrbanApiError(f"Urban API недоступен ({path}): {last_error}")

    def _paginated(self, path: str, params: dict) -> list[dict]:
        """Collect every page of a ``{count, next, results}`` listing."""
        collected: list[dict] = []
        page_params = dict(params, page_size=_PAGE_SIZE)
        for page in range(1, _MAX_PAGES + 1):
            data = self._get(path, dict(page_params, page=page))
            results = data.get("results") or []
            collected.extend(results)
            if not data.get("next") or not results:
                break
        return collected

    # --- catalogue ---------------------------------------------------------------------

    def territory(self, territory_id: int) -> Territory:
        """One territory by id (with its parent) — the entry point for path resolution."""
        key = ("territory", int(territory_id))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        # The single-territory endpoint carries the full polygon; only the scalar fields are
        # kept, and the result is cached, so the payload cost is paid once per territory.
        data = self._get(f"/api/v1/territory/{int(territory_id)}")
        territory = Territory.from_api(data)
        self._cache.set(key, territory)
        return territory

    def children(self, parent_id: int, *, all_levels: bool = False) -> list[Territory]:
        """Direct children of a territory (or its whole subtree with ``all_levels``)."""
        key = ("children", int(parent_id), all_levels)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        params: dict = {"parent_id": int(parent_id)}
        if all_levels:
            params["get_all_levels"] = "true"
        found = [
            Territory.from_api(item)
            for item in self._paginated("/api/v1/territories_without_geometry", params)
        ]
        self._cache.set(key, found)
        return found

    def regions(self) -> list[Territory]:
        """The 89 subjects of the federation — level 2, the children of "Россия"."""
        return self.children(COUNTRY_TERRITORY_ID)

    def find_by_name(
        self,
        query: str,
        parent_id: int = COUNTRY_TERRITORY_ID,
        *,
        all_levels: bool = True,
        limit: int = 20,
    ) -> list[Territory]:
        """Server-side substring search over territory names, scoped to a subtree.

        Backs both the automatic match during ingestion and the admin autocomplete: the Urban
        API filters by ``name`` itself, so neither has to enumerate a 100k-node tree. The
        result keeps the parent name, which is what makes ambiguous municipality names
        ("Кировский район") distinguishable.
        """
        cleaned = (query or "").strip()
        if not cleaned:
            return []
        key = ("find", cleaned.lower(), int(parent_id), all_levels)
        cached = self._cache.get(key)
        if cached is None:
            params: dict = {"parent_id": int(parent_id), "name": cleaned}
            if all_levels:
                params["get_all_levels"] = "true"
            cached = [
                Territory.from_api(item)
                for item in self._paginated(
                    "/api/v1/territories_without_geometry", params
                )
            ]
            self._cache.set(key, cached)
        return cached[:limit] if limit else cached

    def ancestor_path(self, territory_id: int) -> list[int]:
        """Ids from the root down to the territory itself, e.g. ``[12639, 1, 54]``.

        Stored on every fragment as ``territory_path`` so that "everything in force in
        Vyborg" is one indexed ``MatchAny`` instead of a tree walk per query.
        """
        key = ("path", int(territory_id))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        path: list[int] = []
        current: int | None = int(territory_id)
        for _ in range(_MAX_ANCESTOR_DEPTH):
            if current is None:
                break
            path.append(current)
            node = self.territory(current)
            current = node.parent_id
            if (
                current in path
            ):  # defensive: a cycle would otherwise spin to the depth cap
                break
        path.reverse()
        self._cache.set(key, path)
        return path
