"""Request priorities for the shared text LLM; origin belongs to each client."""

from __future__ import annotations

from functools import lru_cache
from threading import Lock

import structlog

from src.api_clients.base import LlmError

log = structlog.get_logger(__name__)

DEFAULT_PRIORITIES = {
    "ingest.structure": 100,
    "ingest.references": 100,
    "ingest.relations": 100,
    "ingest.version": 100,
    "ingest.amendments": 100,
    "ingest.vision": 100,
    "ingest.user": 20,
    "query": -100,
}


@lru_cache(maxsize=32)
def _priorities(overrides: str) -> dict[str, int]:
    values = DEFAULT_PRIORITIES.copy()
    invalid = []
    for entry in overrides.split(","):
        if not entry.strip():
            continue
        key, sep, value = entry.partition("=")
        key = key.strip()
        try:
            if not sep or key not in values:
                raise ValueError
            values[key] = int(value.strip())
        except ValueError:
            invalid.append(entry)
    if invalid:
        log.warning("llm_priority_invalid_overrides", entries=invalid)
    return values


class RequestPriority:
    def __init__(self, settings, *, user_document: bool = False):
        self.enabled = settings.llm_request_priority_enabled
        self.values = _priorities(settings.llm_request_priorities)
        self.user_document = user_document
        self._unsupported = False
        self._missing_kind = False
        self._lock = Lock()

    def resolve(self, kind: str | None) -> int:
        if kind is None:
            with self._lock:
                if not self._missing_kind:
                    log.warning("llm_priority_missing_kind")
                    self._missing_kind = True
            return 0
        if kind not in self.values:
            raise LlmError(f"Неизвестный тип LLM-запроса: {kind}")
        if not self.enabled or self._unsupported:
            return 0
        if self.user_document and kind.startswith("ingest."):
            kind = "ingest.user"
        return self.values[kind]

    def post(self, client, url: str, body: dict, *, kind: str | None, **kwargs):
        priority = self.resolve(kind)
        if priority:
            body = {**body, "priority": priority}
        response = client.post(url, json=body, **kwargs)
        message = response.text.lower() if response.status_code == 400 else ""
        if (
            priority
            and "priority" in message
            and any(
                phrase in message
                for phrase in (
                    "not enabled",
                    "not supported",
                    "unsupported",
                    "disabled",
                    "does not support",
                )
            )
        ):
            with self._lock:
                if not self._unsupported:
                    self._unsupported = True
                    log.warning("llm_priority_unsupported", url=url)
            body = {key: value for key, value in body.items() if key != "priority"}
            response = client.post(url, json=body, **kwargs)
        return response
