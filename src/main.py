import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from src.__version__ import VERSION
from src.admin_service.router import router as admin_router
from src.common.auth import require_admin
from src.common.middlewares import RequestLoggingMiddleware
from src.dependencies import init_dependencies
from src.dvd_service.routers import (
    direct_documents_router,
    documents_router,
    library_router,
    search_router,
    tagging_router,
    user_documents_router,
)
from src.mcp_server.app import mcp_app
from src.system_service.routers import system_router


async def _tagging_backfill_loop(deps) -> None:
    """Sweep documents left without an administrative scope: once after startup, then on a timer.

    Deliberately *not* part of ``init_dependencies``: the sweep talks to the LLM and the Urban
    API, and startup must never block on either (the dependency init has the same rule for its
    own cleanups). The timer matters as much as the startup run — without it, a document that
    arrived during an Urban API outage would stay untagged until someone restarted the service.
    """
    log = structlog.get_logger("tagging-backfill")
    settings = deps.settings
    await asyncio.sleep(settings.tagging_backfill_delay)
    while True:
        try:
            result = await run_in_threadpool(deps.tagging_backfill.run)
            if result.get("processed"):
                log.info(
                    "tagging_backfill_sweep",
                    **{key: result[key] for key in ("processed", "tagged", "failed")},
                )
        except Exception as exc:  # noqa: BLE001 — the loop outlives any single failure
            log.warning("tagging_backfill_sweep_failed", error=str(exc))
        if settings.tagging_backfill_interval <= 0:
            return
        await asyncio.sleep(settings.tagging_backfill_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps = init_dependencies()
    log = structlog.get_logger()
    log.info(f"Started server version {VERSION}")
    async with deps.service_auth:
        await deps.service_auth.get_access_token()
        deps.urban_api.bind_event_loop(asyncio.get_running_loop())
        # Kafka outbox publisher (no-op when DVD_KAFKA_BOOTSTRAP_SERVERS is not set)
        await deps.publisher.start()
        backfill_task = asyncio.create_task(_tagging_backfill_loop(deps))
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            backfill_task.cancel()
            await deps.publisher.stop()
            deps.urban_api.close()


app = FastAPI(
    title="DVD IDU — векторная база нормативных документов",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestLoggingMiddleware)
# documents_router and library_router gate each route themselves: reading the shared corpus
# is open to any authenticated caller, writing to it is not.
app.include_router(documents_router)
app.include_router(direct_documents_router, dependencies=[Depends(require_admin)])
app.include_router(search_router)
app.include_router(library_router)
app.include_router(tagging_router, dependencies=[Depends(require_admin)])
app.include_router(user_documents_router)
app.include_router(system_router, dependencies=[Depends(require_admin)])
app.include_router(admin_router)
app.mount("/mcp", mcp_app)


@app.get("/")
async def read_root():
    return RedirectResponse("/docs")


@app.get("/ping")
async def ping_server():
    return {"ping": "pong"}
