from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from src.__version__ import VERSION
from src.common.middlewares import RequestLoggingMiddleware
from src.dependencies import init_dependencies
from src.dvd_service.routers import documents_router, library_router, search_router
from src.mcp_server.app import mcp_app
from src.system_service.routers import system_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps = init_dependencies()
    log = structlog.get_logger()
    log.info(f"Started server version {VERSION}")
    # Kafka outbox publisher (no-op when DVD_KAFKA_BOOTSTRAP_SERVERS is not set)
    await deps.publisher.start()
    try:
        async with mcp_app.lifespan(app):
            yield
    finally:
        await deps.publisher.stop()


app = FastAPI(
    title="DVD IDU — векторная база нормативных документов",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(RequestLoggingMiddleware)
app.include_router(documents_router)
app.include_router(search_router)
app.include_router(library_router)
app.include_router(system_router)
app.mount("/mcp", mcp_app)


@app.get("/")
async def read_root():
    return RedirectResponse("/docs")


@app.get("/ping")
async def ping_server():
    return {"ping": "pong"}
