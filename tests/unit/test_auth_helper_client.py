"""Unit tests for the IDU auth helper client — the admin panel's way to a Keycloak token."""

from __future__ import annotations

import httpx
import pytest

from src.api_clients.auth_helper_client import (
    API_KEY_HEADER,
    AuthHelperClient,
    AuthHelperError,
)


@pytest.fixture(autouse=True)
def _patch_async_client(monkeypatch):
    """Route every AsyncClient the code under test creates through the test's transport."""

    original = httpx.AsyncClient
    holder: dict = {}

    def factory(*args, **kwargs):
        if "transport" not in kwargs and holder.get("transport") is not None:
            kwargs["transport"] = holder["transport"]
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return holder


def _serve(holder, handler) -> AuthHelperClient:
    holder["transport"] = httpx.MockTransport(handler)
    return AuthHelperClient("https://auth.example.com/", "key-1")


async def test_posts_credentials_with_the_server_side_api_key(_patch_async_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get(API_KEY_HEADER)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"access_token": "token-1", "expires_in": 300})

    client = _serve(_patch_async_client, handler)

    assert await client.issue_token("admin", "secret") == "token-1"
    assert seen["url"] == "https://auth.example.com/api/token"
    assert seen["key"] == "key-1", "the API key must never reach the browser"
    assert '"username":"admin"' in seen["body"]
    assert "openid profile email" in seen["body"]


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_credentials_surface_as_401(_patch_async_client, status):
    client = _serve(_patch_async_client, lambda request: httpx.Response(status))

    with pytest.raises(AuthHelperError) as error:
        await client.issue_token("admin", "wrong")

    assert error.value.status_code == 401


async def test_helper_failure_is_not_reported_as_a_bad_password(_patch_async_client):
    client = _serve(_patch_async_client, lambda request: httpx.Response(500))

    with pytest.raises(AuthHelperError) as error:
        await client.issue_token("admin", "secret")

    assert error.value.status_code == 502


async def test_unreachable_helper_is_reported_as_such(_patch_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _serve(_patch_async_client, handler)

    with pytest.raises(AuthHelperError) as error:
        await client.issue_token("admin", "secret")

    assert error.value.status_code == 502
    assert "unreachable" in str(error.value)


async def test_a_body_without_a_token_is_refused(_patch_async_client):
    client = _serve(_patch_async_client, lambda request: httpx.Response(200, json={}))

    with pytest.raises(AuthHelperError):
        await client.issue_token("admin", "secret")


async def test_unconfigured_client_asks_for_the_variables():
    client = AuthHelperClient(None, None)

    assert client.configured is False
    with pytest.raises(AuthHelperError) as error:
        await client.issue_token("admin", "secret")

    assert error.value.status_code == 503
    assert "DVD_AUTH_HELPER_URL" in str(error.value)


@pytest.mark.parametrize(
    "base,api_key,configured",
    [
        ("https://auth.example.com", "key", True),
        ("https://auth.example.com", None, False),
        (None, "key", False),
    ],
)
def test_both_halves_are_needed(base, api_key, configured):
    assert AuthHelperClient(base, api_key).configured is configured
