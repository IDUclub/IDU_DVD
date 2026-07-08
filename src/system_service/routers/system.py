"""System endpoints: download the application log file and read/write runtime settings."""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.dependencies import Dependencies
from src.system_service.controllers import SystemController

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/system", tags=["system"])


class SettingItem(BaseModel):
    """One configuration variable, in both env-var and field form."""

    field: str  # pydantic field name, e.g. "search_limit"
    env: str  # environment-variable name, e.g. "DVD_SEARCH_LIMIT"
    value: Any  # current value (sensitive values are masked as "***")
    restart_required: bool  # change takes effect only after a restart
    sensitive: bool  # value is masked on read / not echoed on write


class SettingsResponse(BaseModel):
    """Current effective configuration (the ``DVD_`` contract), secrets masked."""

    effective_collection: str
    registry_prefix: str
    vector_size: (
        int  # actual dimension in use (auto-detected from the vectorizer at startup)
    )
    embeddings_provider: str
    env_file: str
    settings: list[SettingItem]


class EnvUpdateRequest(BaseModel):
    """Variables to persist. Keys may be env names (``DVD_SEARCH_LIMIT``) or field names."""

    updates: dict[str, Any] = Field(
        ...,
        description="Карта переменная -> значение (имя вида DVD_X или поле x).",
        examples=[{"DVD_SEARCH_LIMIT": 20, "DVD_SEMANTIC_MERGE_MAX_PASSES": 1}],
    )


class EnvUpdateResponse(BaseModel):
    """Result of a settings write: what was persisted and what applied without a restart."""

    updated: list[SettingItem]  # every persisted variable (masked)
    live_applied: list[str]  # fields applied to the running app immediately
    restart_required: list[str]  # fields persisted but effective only after a restart
    restart_needed: bool  # true if any updated field needs a restart
    env_file: str


@router.get("/logs")
async def download_logs(
    date: date | None = Query(
        default=None,
        description="Вернуть только логи за этот день (формат YYYY-MM-DD).",
    ),
    request_id: str | None = Query(
        default=None,
        description="Вернуть только логи с указанным request_id.",
    ),
    system: SystemController = Depends(Dependencies.get_system),
):
    """Download application logs as a human-readable file.

    Без параметров возвращает весь файл; ``date`` и ``request_id`` сужают выборку
    (их можно комбинировать).
    """
    if not system.log_file_exists():
        raise HTTPException(404, "Лог-файл ещё не создан")

    filename = system.build_filename(date, request_id)
    return StreamingResponse(
        system.iter_formatted_logs(date, request_id),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/settings", response_model=SettingsResponse)
async def read_settings(
    system: SystemController = Depends(Dependencies.get_system),
) -> SettingsResponse:
    """Read the current effective configuration (``DVD_`` variables), secrets masked.

    ``vector_size`` reflects the dimension auto-detected from the vectorizer at startup — the
    quickest way to confirm Qdrant is storing 2048-d vectors.
    """
    return SettingsResponse(**system.settings_snapshot())


@router.put("/settings", response_model=EnvUpdateResponse)
async def write_settings(
    body: EnvUpdateRequest = Body(...),
    system: SystemController = Depends(Dependencies.get_system),
) -> EnvUpdateResponse:
    """Persist ``DVD_`` variables to the ``.env`` file and apply the runtime-tunable ones live.

    Runtime-tunable settings (search/window/merge/reference toggles, Ollama/vectorizer
    endpoints) take effect on the next request or ingest. Structural ones (Qdrant collection
    and dimension, embeddings provider, Redis/Kafka wiring, logging, ingest concurrency) are
    written to ``.env`` but require a restart — they are listed in ``restart_required``.
    Unknown variables are rejected.
    """
    try:
        result = system.update_env(body.updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return EnvUpdateResponse(**result)
