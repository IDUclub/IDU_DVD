from __future__ import annotations

import time

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken

from src.common.auth import (
    get_current_user_id,
    get_effective_user_id,
    keycloak_token_verifier,
    require_authenticated,
    require_service_token,
    service_token_verifier,
)


@pytest.fixture(autouse=True)
def _fake_token_verifier(monkeypatch):
    async def fake_verify_token(token):
        if token == "user-token":
            return AccessToken(
                token=token,
                client_id="frontend",
                scopes=[],
                claims={"sub": "user-1", "preferred_username": "user"},
            )
        if token == "service-token":
            return AccessToken(
                token=token,
                client_id="service",
                scopes=[],
                claims={
                    "sub": "service-subject",
                    "preferred_username": "service-account-test",
                },
            )
        if token == "expired-token":
            return AccessToken(
                token=token,
                client_id="frontend",
                scopes=[],
                expires_at=int(time.time()) - 1,
                claims={"sub": "user-1", "preferred_username": "user"},
            )
        if token == "no-sub-token":
            return AccessToken(
                token=token,
                client_id="frontend",
                scopes=[],
                claims={"preferred_username": "user"},
            )
        return None

    monkeypatch.setattr(keycloak_token_verifier, "verify_token", fake_verify_token)


async def _resolve(token: str, user_id: str | None = None) -> str:
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return await get_current_user_id(credentials, user_id)


async def test_user_token_uses_subject_without_user_id_header():
    assert await _resolve("user-token") == "user-1"


async def test_user_token_ignores_spoofed_user_id_header():
    assert await _resolve("user-token", "another-user") == "user-1"


async def test_service_token_uses_user_id_header():
    assert await _resolve("service-token", "user-2") == "user-2"


async def test_service_token_requires_user_id_header():
    with pytest.raises(HTTPException) as error:
        await _resolve("service-token")

    assert error.value.status_code == 401
    assert error.value.detail == "X-User-Id header is required"


@pytest.mark.parametrize("token", ["invalid-token", "no-sub-token"])
async def test_invalid_user_identity_is_rejected(token):
    with pytest.raises(HTTPException) as error:
        await _resolve(token)

    assert error.value.status_code == 401


async def test_service_verifier_accepts_service_token():
    access_token = await service_token_verifier.verify_token("service-token")

    assert access_token is not None
    assert access_token.claims["sub"] == "service-subject"


async def test_service_verifier_rejects_user_token():
    with pytest.raises(AuthorizationError):
        await service_token_verifier.verify_token("user-token")


def _request() -> Request:
    """A bare request — no admin session cookie, so only a bearer token can authenticate it."""

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/documents",
            "headers": [],
            "query_string": b"",
        }
    )


def _credentials(token: str | None) -> HTTPAuthorizationCredentials | None:
    if token is None:
        return None
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.parametrize("token", ["user-token", "service-token"])
async def test_authenticated_accepts_user_and_service_tokens(token):
    assert await require_authenticated(_request(), _credentials(token)) is None


@pytest.mark.parametrize(
    "token,detail",
    [
        (None, "Bearer token is required"),
        ("invalid-token", "Invalid bearer token"),
        ("expired-token", "Bearer token has expired"),
    ],
)
async def test_authenticated_rejects_missing_and_stale_tokens(token, detail):
    with pytest.raises(HTTPException) as error:
        await require_authenticated(_request(), _credentials(token))

    assert error.value.status_code == 401
    assert error.value.detail == detail


async def test_expired_token_is_rejected_for_user_identity():
    with pytest.raises(HTTPException) as error:
        await _resolve("expired-token")

    assert error.value.status_code == 401
    assert error.value.detail == "Bearer token has expired"


async def test_service_gate_still_refuses_a_user_token():
    with pytest.raises(HTTPException) as error:
        await require_service_token(_request(), _credentials("user-token"))

    assert error.value.status_code == 401
    assert error.value.detail == "Invalid service token"


async def test_service_gate_accepts_a_service_token():
    assert (
        await require_service_token(_request(), _credentials("service-token")) is None
    )


async def test_effective_user_ignores_a_spoofed_header_on_a_user_token():
    resolved = await get_effective_user_id(
        _request(), _credentials("user-token"), "another-user"
    )

    assert resolved == "user-1"


async def test_effective_user_lets_a_service_act_for_someone():
    resolved = await get_effective_user_id(
        _request(), _credentials("service-token"), "user-2"
    )

    assert resolved == "user-2"


async def test_effective_user_is_none_for_a_service_without_a_header():
    """Shared-corpus access, but nothing to open a private index with."""

    resolved = await get_effective_user_id(
        _request(), _credentials("service-token"), None
    )

    assert resolved is None
