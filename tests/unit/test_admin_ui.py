"""Authentication and delivery tests for the server-rendered admin UI."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin_service.router import router
from src.common.config import Settings
from src.dependencies import Dependencies


def _client(password: str | None = "secret") -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[Dependencies.get_settings] = lambda: Settings(
        admin_password=password
    )
    return TestClient(app)


def test_admin_redirects_to_login_without_cookie():
    with _client() as client:
        response = client.get("/admin/ui", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/ui/login"


def test_login_sets_http_only_cookie_and_opens_ui():
    with _client() as client:
        response = client.post(
            "/admin/ui/login", data={"password": "secret"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "httponly" in response.headers["set-cookie"].lower()
        page = client.get("/admin/ui")
        assert page.status_code == 200
        assert "DVD Admin" in page.text and 'data-theme="dark"' in page.text


def test_wrong_password_does_not_create_session():
    with _client() as client:
        response = client.post("/admin/ui/login", data={"password": "wrong"})
        assert response.status_code == 200
        assert "Неверный пароль" in response.text
        assert "dvd_admin_session" not in response.headers.get("set-cookie", "")


def test_missing_password_explains_configuration():
    with _client(None) as client:
        response = client.get("/admin/ui/login")
        assert response.status_code == 200
        assert "DVD_ADMIN_PASSWORD" in response.text


def test_static_assets_are_served_locally():
    with _client() as client:
        css = client.get("/admin/ui/assets/admin.css")
        js = client.get("/admin/ui/assets/admin.js")
        assert css.status_code == js.status_code == 200
        assert "--accent" in css.text and "loadDocuments" in js.text
