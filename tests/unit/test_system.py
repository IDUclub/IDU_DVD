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


# --------------------------------------------------------------------------------------
# Settings read/write (the DVD_ environment contract)
# --------------------------------------------------------------------------------------
@pytest.fixture
def settings_controller(tmp_path):
    s = Settings(qdrant_api_key="supersecret", log_dir=str(tmp_path))
    return SystemController(s, env_path=tmp_path / ".env")


class TestSettingsController:
    def test_snapshot_masks_secret_and_reports_vector_size(self, settings_controller):
        snap = settings_controller.settings_snapshot()
        assert snap["vector_size"] == settings_controller._settings.vector_size
        items = {i["field"]: i for i in snap["settings"]}
        # the secret is masked and flagged
        assert items["qdrant_api_key"]["value"] == "***"
        assert items["qdrant_api_key"]["sensitive"] is True
        # structural field is flagged as restart-required; env-name mapping is exposed
        assert items["vector_size"]["restart_required"] is True
        assert items["search_limit"]["env"] == "DVD_SEARCH_LIMIT"

    def test_update_live_field_applies_and_persists(
        self, settings_controller, tmp_path
    ):
        res = settings_controller.update_env({"DVD_SEARCH_LIMIT": 20})
        assert res["live_applied"] == ["search_limit"]
        assert res["restart_needed"] is False
        assert settings_controller._settings.search_limit == 20  # applied in-memory
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "DVD_SEARCH_LIMIT=20" in env_text  # persisted for restart

    def test_update_structural_field_requires_restart(self, settings_controller):
        before = settings_controller._settings.vector_size
        res = settings_controller.update_env({"vector_size": 1024})
        assert res["restart_required"] == ["vector_size"]
        assert res["restart_needed"] is True
        # the live value is left untouched — mutating it would misrepresent the running
        # Qdrant collection, which still has the old dimension until a restart
        assert settings_controller._settings.vector_size == before

    def test_field_name_and_env_name_both_accepted(self, settings_controller):
        settings_controller.update_env({"semantic_merge_max_passes": 3})
        assert settings_controller._settings.semantic_merge_max_passes == 3
        settings_controller.update_env({"DVD_SEMANTIC_MERGE_MAX_PASSES": 2})
        assert settings_controller._settings.semantic_merge_max_passes == 2

    def test_unknown_variable_rejected(self, settings_controller):
        with pytest.raises(ValueError):
            settings_controller.update_env({"DVD_NOPE": "x"})

    def test_empty_updates_rejected(self, settings_controller):
        with pytest.raises(ValueError):
            settings_controller.update_env({})


@pytest.fixture
def settings_client(settings_controller):
    app = FastAPI()
    app.include_router(system_router)
    app.dependency_overrides[Dependencies.get_system] = lambda: settings_controller
    with TestClient(app) as c:
        yield c


class TestSettingsEndpoint:
    def test_get_settings(self, settings_client):
        resp = settings_client.get("/system/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vector_size"] >= 1
        fields = {i["field"] for i in data["settings"]}
        assert {"search_limit", "vector_size", "qdrant_api_key"} <= fields
        secret = next(i for i in data["settings"] if i["field"] == "qdrant_api_key")
        assert secret["value"] == "***"

    def test_put_settings_applies_live(self, settings_client, settings_controller):
        resp = settings_client.put(
            "/system/settings", json={"updates": {"DVD_SEARCH_LIMIT": 25}}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["live_applied"] == ["search_limit"]
        assert body["restart_needed"] is False
        assert settings_controller._settings.search_limit == 25

    def test_put_structural_field_reports_restart(self, settings_client):
        resp = settings_client.put(
            "/system/settings", json={"updates": {"DVD_VECTOR_SIZE": 1024}}
        )
        assert resp.status_code == 200
        assert resp.json()["restart_required"] == ["vector_size"]

    def test_put_unknown_returns_422(self, settings_client):
        resp = settings_client.put(
            "/system/settings", json={"updates": {"DVD_NOPE": "1"}}
        )
        assert resp.status_code == 422
