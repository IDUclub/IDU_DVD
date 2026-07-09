"""ASGI application of the MCP server, mounted into FastAPI (see `src/main.py`)."""

from __future__ import annotations

from src.mcp_server.server import mcp

mcp_app = mcp.http_app(path="/", host_origin_protection=False)
