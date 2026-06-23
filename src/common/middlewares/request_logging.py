"""Request-logging middleware.

Logs every incoming request (start + completion) and binds a ``request_id`` into
``structlog.contextvars`` for the duration of the request, so *every* log line emitted while
handling it — not just these two — carries the same id. The id is taken from the inbound
``X-Request-ID`` header when present (cross-service tracing) or generated otherwise, and is
echoed back in the response ``X-Request-ID`` header.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

log = structlog.get_logger("http")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Bind a request_id, log the call, and echo the id back in the response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        log.info(
            "request_started",
            method=request.method,
            path=request.url.path,
            query=request.url.query or None,
            client=request.client.host if request.client else None,
        )

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            structlog.contextvars.clear_contextvars()
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.info(
            "request_finished",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers[REQUEST_ID_HEADER] = request_id
        structlog.contextvars.clear_contextvars()
        return response
