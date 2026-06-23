from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from src.__version__ import VERSION
from src.dependencies import init_dependencies
from src.dvd_service.routers import documents_router, search_router
from src.mcp_server.app import mcp_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_dependencies()
    log = structlog.get_logger()
    log.info(f"Started server version {VERSION}")
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(
    title="DVD IDU — векторная база нормативных документов",
    version=VERSION,
    lifespan=lifespan,
)
app.include_router(documents_router)
app.include_router(search_router)
app.mount("/mcp", mcp_app)


@app.get("/")
async def read_root():
    return RedirectResponse("/docs")


@app.get("/ping")
async def ping_server():
    return {"ping": "pong"}
