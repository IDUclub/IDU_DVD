from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken

from src.common.auth import (
    get_current_user_id,
    keycloak_token_verifier,
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
