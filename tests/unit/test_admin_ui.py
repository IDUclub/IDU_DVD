"""Authentication and delivery tests for the server-rendered admin UI."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.server.auth import AccessToken

from src.admin_service.router import router
from src.api_clients import AuthHelperError, Territory, UrbanApiError
from src.common import auth
from src.dependencies import Dependencies

ADMIN_CLAIMS = {
    "sub": "admin-1",
    "preferred_username": "admin",
    "realm_access": {"roles": ["ADMIN"]},
}
USER_CLAIMS = {
    "sub": "user-1",
    "preferred_username": "user",
    "realm_access": {"roles": ["STAFF"]},
}


class FakeAuthHelper:
    """Stands in for the IDU auth helper: known credentials map to a token, nothing else."""

    def __init__(self, configured: bool = True, unreachable: bool = False):
        self._configured = configured
        self.unreachable = unreachable
        self.calls: list[tuple[str, str]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def issue_token(self, username: str, password: str) -> str:
        self.calls.append((username, password))
        if not self._configured:
            raise AuthHelperError("not configured", status_code=503)
        if self.unreachable:
            raise AuthHelperError("unreachable")
        if (username, password) == ("admin", "right"):
            return "admin-token"
        if (username, password) == ("user", "right"):
            return "user-token"
        raise AuthHelperError("invalid username or password", status_code=401)


@pytest.fixture(autouse=True)
def _fake_token_verifier(monkeypatch):
    """Verify the tokens the fake helper issues, without a Keycloak to check them against."""

    async def fake_verify_token(token):
        claims = {"admin-token": ADMIN_CLAIMS, "user-token": USER_CLAIMS}.get(token)
        if claims is None:
            return None
        return AccessToken(token=token, client_id="frontend", scopes=[], claims=claims)

    monkeypatch.setattr(auth.keycloak_token_verifier, "verify_token", fake_verify_token)


def _client(auth_helper: FakeAuthHelper | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[Dependencies.get_auth_helper] = (
        lambda: auth_helper or FakeAuthHelper()
    )
    return TestClient(app)


def _login(client: TestClient, username: str = "admin", password: str = "right"):
    return client.post(
        "/admin/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_admin_redirects_to_login_without_cookie():
    with _client() as client:
        response = client.get("/admin/ui", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/ui/login"


def test_login_stores_the_issued_token_and_opens_ui():
    helper = FakeAuthHelper()
    with _client(helper) as client:
        response = _login(client)
        assert response.status_code == 303
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "admin-token" in cookie
        assert helper.calls == [("admin", "right")]
        page = client.get("/admin/ui")
        assert page.status_code == 200
        assert "DVD Admin" in page.text and 'data-theme="dark"' in page.text


def test_user_without_the_admin_role_is_turned_away():
    """The credentials are valid — the entitlement is not."""

    with _client() as client:
        response = _login(client, username="user")
        assert response.status_code == 200
        assert "нет прав администратора" in response.text
        assert "dvd_admin_session" not in response.headers.get("set-cookie", "")
        assert client.get("/admin/ui", follow_redirects=False).status_code == 303


def test_wrong_credentials_do_not_create_a_session():
    with _client() as client:
        response = _login(client, password="wrong")
        assert response.status_code == 200
        assert "Неверный логин или пароль" in response.text
        assert "dvd_admin_session" not in response.headers.get("set-cookie", "")


def test_unreachable_helper_says_so_without_blaming_the_password():
    with _client(FakeAuthHelper(unreachable=True)) as client:
        response = _login(client)
        assert response.status_code == 200
        assert "Сервис авторизации недоступен" in response.text


def test_missing_configuration_names_the_variables():
    with _client(FakeAuthHelper(configured=False)) as client:
        response = client.get("/admin/ui/login")
        assert response.status_code == 200
        assert "DVD_AUTH_HELPER_URL" in response.text
        assert "DVD_AUTH_HELPER_API_KEY" in response.text


def test_logout_clears_the_session():
    with _client() as client:
        _login(client)
        client.post("/admin/ui/logout", follow_redirects=False)
        assert client.get("/admin/ui", follow_redirects=False).status_code == 303


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
    _login(client)
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
        _login(client)
        markup = client.get("/admin/ui").text
        assert "level-filter" in markup and "territory-filter" in markup
        assert "pending-filter" in markup and "run-backfill" in markup
        assert "meta-territory" in page and "/tagging/backfill" in page
        assert "/admin/ui/territories" in page
