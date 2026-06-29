"""HTTP routers package: FastAPI endpoints grouped by concern.

Endpoints live in dedicated modules (``documents``, ``search``, ``library``); this file only
marks the package and re-exports each module's ``router`` under a descriptive name for mounting.
"""

from src.dvd_service.routers.documents import router as documents_router  # noqa: F401
from src.dvd_service.routers.library import router as library_router  # noqa: F401
from src.dvd_service.routers.search import router as search_router  # noqa: F401

__all__ = ["documents_router", "search_router", "library_router"]
