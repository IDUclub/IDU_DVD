"""Integration: the FastAPI application boots against the live stack.

Drives the real lifespan (which calls ``init_dependencies`` → connects to Qdrant + Redis and
mounts the MCP app) via TestClient, then checks the liveness endpoints. Ollama is not needed
for boot, so only Qdrant + Redis are required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_app_boots_and_responds(require_qdrant, require_redis, reset_dependencies):
    from src.main import app

    with TestClient(app) as client:  # entering runs the lifespan (init_dependencies)
        assert client.get("/ping").json() == {"ping": "pong"}
        root = client.get("/", follow_redirects=False)
        assert root.status_code in (307, 308)  # redirect to /docs
