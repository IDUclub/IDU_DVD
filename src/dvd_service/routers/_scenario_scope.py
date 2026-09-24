"""Shared-corpus listings narrowed to a scenario's territories (``scenario_id`` query param)."""

from __future__ import annotations

from functools import partial

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from src.api_clients import ScenarioNotFound, UrbanApiError
from src.dependencies import Dependencies
from src.dvd_service.services.dvd_service import scenario_listing_condition

SCENARIO_ID_DESCRIPTION = (
    "Urban API scenario id: only documents in force where the scenario is (under its "
    "project boundary, inside and above); territory_ids replace it"
)
SCENARIO_FILTER_DESCRIPTION = "false ignores scenario_id"


async def scenario_condition(
    scenario_id: str | None,
    user_id: str | None,
    territory_ids: list[int] | None = None,
    enabled: bool = True,
):
    """The Qdrant condition for ``scenario_id``, with the HTTP error mapping of search."""
    if not scenario_id:
        return None
    if not user_id:
        raise HTTPException(
            401,
            "scenario_id needs a user token, or a service token naming the user in "
            "X-User-Id",
        )
    try:
        return await run_in_threadpool(
            partial(
                scenario_listing_condition,
                Dependencies.get_territory(),
                scenario_id,
                user_id,
                territory_ids=territory_ids,
                enabled=enabled,
            )
        )
    except ScenarioNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except UrbanApiError as exc:
        raise HTTPException(502, str(exc)) from exc
