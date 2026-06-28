"""Unit tests for the system service (log retrieval) and the request-logging middleware.

Covers: SystemController filtering (by day / request_id / combined) and readable rendering,
the /system/logs endpoint (full file, filtered, 404 when absent), and the middleware that
binds request_id and echoes the X-Request-ID header.
"""

from __future__ import annotations

import datetime
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.common.config import Settings
from src.common.middlewares import REQUEST_ID_HEADER, RequestLoggingMiddleware
from src.dependencies import Dependencies
from src.system_service.controllers import SystemController
from src.system_service.routers import system_router


def _write_log(path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


@pytest.fixture
def log_settings(tmp_path):
    return Settings(log_dir=str(tmp_path), log_file="app.log")


@pytest.fixture
def controller(log_settings, tmp_path):
    entries = [
        {
            "timestamp": "2026-06-20T10:00:00Z",
            "level": "info",
            "logger": "http",
            "request_id": "REQ-A",
            "event": "request_started",
            "method": "GET",
        },
        {
            "timestamp": "2026-06-20T10:00:01Z",
            "level": "info",
            "logger": "http",
            "request_id": "REQ-A",
            "event": "request_finished",
            "status_code": 200,
        },
        {
            "timestamp": "2026-06-21T09:30:00Z",
            "level": "warning",
            "logger": "app",
            "request_id": "REQ-B",
            "event": "something_off",
        },
    ]
    _write_log(tmp_path / "app.log", entries)
    return SystemController(log_settings)


class TestController:
    def test_no_filter_returns_all(self, controller):
        lines = list(controller.iter_formatted_logs())
        assert len(lines) == 3

    def test_filter_by_day(self, controller):
        day = datetime.date(2026, 6, 20)
        lines = list(controller.iter_formatted_logs(day=day))
        assert len(lines) == 2
        assert all("2026-06-20" in ln for ln in lines)

    def test_filter_by_request_id(self, controller):
        lines = list(controller.iter_formatted_logs(request_id="REQ-B"))
        assert len(lines) == 1
        assert "something_off" in lines[0]
        assert "REQ-B" in lines[0]

    def test_filter_combined_day_and_request_id(self, controller):
        day = datetime.date(2026, 6, 20)
        assert list(controller.iter_formatted_logs(day=day, request_id="REQ-B")) == []
        assert (
            len(list(controller.iter_formatted_logs(day=day, request_id="REQ-A"))) == 2
        )

    def test_rendered_line_is_human_readable(self, controller):
        line = list(controller.iter_formatted_logs(request_id="REQ-A"))[0]
        # readable head: timestamp [LEVEL] logger: (request_id=...) event ...
        assert line.startswith("2026-06-20T10:00:00Z [INFO")
        assert "http:" in line and "request_started" in line

    def test_build_filename(self, controller):
        assert controller.build_filename(None, None) == "logs.log"
        assert (
            controller.build_filename(datetime.date(2026, 6, 20), None)
            == "logs_2026-06-20.log"
        )
        assert controller.build_filename(None, "REQ-A") == "logs_REQ-A.log"


@pytest.fixture
def client(controller):
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(system_router)
    app.dependency_overrides[Dependencies.get_system] = lambda: controller
    with TestClient(app) as c:
        yield c


class TestEndpoint:
    def test_download_all(self, client):
        resp = client.get("/system/logs")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        assert len(resp.text.strip().splitlines()) == 3

    def test_download_filtered_by_request_id(self, client):
        resp = client.get("/system/logs", params={"request_id": "REQ-B"})
        assert resp.status_code == 200
        body = resp.text.strip()
        assert "something_off" in body and "REQ-A" not in body

    def test_download_filtered_by_date(self, client):
        resp = client.get("/system/logs", params={"date": "2026-06-20"})
        assert resp.status_code == 200
        assert len(resp.text.strip().splitlines()) == 2

    def test_invalid_date_returns_422(self, client):
        assert (
            client.get("/system/logs", params={"date": "not-a-date"}).status_code == 422
        )

    def test_missing_log_file_returns_404(self, client, controller):
        controller.log_path.unlink()
        assert client.get("/system/logs").status_code == 404


class TestMiddleware:
    def test_sets_request_id_header(self, client):
        resp = client.get("/system/logs")
        assert resp.headers.get(REQUEST_ID_HEADER)

    def test_honors_incoming_request_id(self, client):
        resp = client.get("/system/logs", headers={REQUEST_ID_HEADER: "TRACE-1"})
        assert resp.headers.get(REQUEST_ID_HEADER) == "TRACE-1"
