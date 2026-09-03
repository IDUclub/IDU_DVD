"""CORS wiring: preflight answers and response headers for a browser frontend.

The browser sends an ``OPTIONS`` preflight before any request carrying ``Authorization``; with
no CORS middleware the app answered it ``405`` and every call from the frontend was reported as
a CORS failure. These tests pin both halves: the settings that describe the policy, and the fact
that the application object actually carries it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.common.config import Settings

ORIGIN = "http://10.32.11.17:5173"


def _app(**overrides) -> TestClient:
    """A one-route app wired from ``Settings`` exactly as ``src.main`` wires the real one."""
    settings = Settings(**overrides)
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_origin_regex=settings.cors_allow_origin_regex,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
        expose_headers=settings.cors_expose_headers,
        max_age=settings.cors_max_age,
    )

    @app.get("/user-documents")
    def _user_documents():
        return []

    return TestClient(app)


class TestOriginsSetting:
    def test_open_by_default(self):
        assert Settings().cors_allow_origins == ["*"]
        assert Settings().cors_allow_credentials is True

    def test_comma_separated_form(self):
        s = Settings(cors_allow_origins="http://a:3000, https://b.idulab.ru")
        assert s.cors_allow_origins == ["http://a:3000", "https://b.idulab.ru"]

    def test_json_form(self):
        s = Settings(cors_allow_origins='["http://a:3000","https://b.idulab.ru"]')
        assert s.cors_allow_origins == ["http://a:3000", "https://b.idulab.ru"]

    def test_env_var_accepts_the_comma_separated_form(self, monkeypatch):
        monkeypatch.setenv(
            "DVD_CORS_ALLOW_ORIGINS", "http://a:3000,https://b.idulab.ru"
        )
        assert Settings().cors_allow_origins == ["http://a:3000", "https://b.idulab.ru"]

    def test_trailing_slash_is_dropped(self):
        """A browser sends ``Origin`` without one, and the comparison is verbatim."""
        assert Settings(cors_allow_origins="http://a:3000/").cors_allow_origins == [
            "http://a:3000"
        ]

    def test_expose_headers_reach_the_frontend_js(self):
        assert Settings().cors_expose_headers == ["X-Request-ID", "Content-Disposition"]


class TestPreflight:
    def test_preflight_is_answered_not_405(self):
        response = _app().options(
            "/user-documents",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert (
            "authorization" in response.headers["access-control-allow-headers"].lower()
        )

    def test_wildcard_with_credentials_echoes_the_concrete_origin(self):
        """``Access-Control-Allow-Origin: *`` is refused on a credentialed request."""
        response = _app().get("/user-documents", headers={"Origin": ORIGIN})
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert response.headers["access-control-allow-credentials"] == "true"
        assert (
            "Content-Disposition" in response.headers["access-control-expose-headers"]
        )

    @pytest.mark.parametrize("origin", [ORIGIN, "https://dvd.idulab.ru"])
    def test_explicit_allow_list_admits_its_origins(self, origin):
        client = _app(cors_allow_origins=f"{ORIGIN},https://dvd.idulab.ru")
        response = client.options(
            "/user-documents",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin

    def test_explicit_allow_list_refuses_everything_else(self):
        client = _app(cors_allow_origins="https://dvd.idulab.ru")
        response = client.options(
            "/user-documents",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    def test_origin_regex_admits_a_subdomain(self):
        client = _app(
            cors_allow_origins="https://dvd.idulab.ru",
            cors_allow_origin_regex=r"https://.*\.idulab\.ru",
        )
        response = client.get(
            "/user-documents", headers={"Origin": "https://stand.idulab.ru"}
        )
        assert (
            response.headers["access-control-allow-origin"] == "https://stand.idulab.ru"
        )


class TestApplicationWiring:
    def test_the_real_app_carries_the_cors_middleware(self):
        from src.main import app

        assert any(m.cls is CORSMiddleware for m in app.user_middleware)

    def test_cors_headers_survive_an_error_response(self):
        from src.main import app

        client = TestClient(app)
        response = client.get("/no-such-route", headers={"Origin": ORIGIN})
        assert response.status_code == 404
        assert response.headers["access-control-allow-origin"] == ORIGIN
