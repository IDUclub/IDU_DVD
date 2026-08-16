"""Authentication and delivery tests for the server-rendered admin UI."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin_service.router import router
from src.api_clients import Territory, UrbanApiError
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
        assert "overall-progress" in css.text and "task-progress" in css.text
        assert "uploadRequest" in js.text and "overall_progress" in js.text


class FakeUrbanApi:
    """Stands in for the Urban API behind the panel's territory autocomplete."""

    def __init__(self, broken: bool = False):
        self.broken = broken
        self.queries = []

    def find_by_name(self, query, limit=20, **_kwargs):
        self.queries.append((query, limit))
        if self.broken:
            raise UrbanApiError("connection refused")
        return [
            Territory(
                54,
                "Выборгский муниципальный район",
                3,
                2,
                "Муниципальное образование",
                1,
                "Ленинградская область",
            )
        ]


def _authenticated_client(urban_api) -> TestClient:
    client = _client()
    Dependencies.reset()
    fields = {name: object() for name in Dependencies._FIELDS}
    fields["urban_api"] = urban_api
    Dependencies().set(**fields)
    client.post("/admin/ui/login", data={"password": "secret"})
    return client


def test_territory_search_requires_a_session():
    with _client() as client:
        response = client.get("/admin/ui/territories?query=Выборг")
        assert response.status_code == 401


def test_territory_search_returns_candidates_with_their_parent():
    """The parent is what lets an admin tell two identically named districts apart."""
    urban_api = FakeUrbanApi()
    with _authenticated_client(urban_api) as client:
        response = client.get("/admin/ui/territories?query=Выборг")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["territories"][0]["territory_id"] == 54
        assert body["territories"][0]["parent_name"] == "Ленинградская область"
        assert body["territories"][0]["document_level"] == "municipal"
    Dependencies.reset()


def test_territory_search_reports_an_urban_api_outage():
    with _authenticated_client(FakeUrbanApi(broken=True)) as client:
        response = client.get("/admin/ui/territories?query=Выборг")
        assert response.status_code == 502
        assert "Urban API" in response.json()["detail"]
    Dependencies.reset()


def test_panel_exposes_the_scope_controls():
    with _client() as client:
        page = client.get("/admin/ui/assets/admin.js").text
        markup = (
            client.post("/admin/ui/login", data={"password": "secret"})
            and client.get("/admin/ui").text
        )
        assert "level-filter" in markup and "territory-filter" in markup
        assert "pending-filter" in markup and "run-backfill" in markup
        assert "meta-territory" in page and "/tagging/backfill" in page
        assert "/admin/ui/territories" in page
