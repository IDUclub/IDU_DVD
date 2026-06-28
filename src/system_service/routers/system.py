"""System endpoints: download the application log file (optionally filtered)."""

from __future__ import annotations

from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.dependencies import Dependencies
from src.system_service.controllers import SystemController

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/system", tags=["system"])


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
