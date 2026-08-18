from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Generator

import httpx
from fastapi import Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_http_headers
from idu_service_auth import KeycloakTokenClient, KeycloakTokenConfig

from src.common.config import Settings
from src.common.config import settings as app_settings

USER_ID_HEADER = "X-User-Id"
SERVICE_ACCOUNT_PREFIX = "service-account-"
ADMIN_SESSION_COOKIE = "dvd_admin_session"
bearer_scheme = HTTPBearer(auto_error=True)
optional_bearer_scheme = HTTPBearer(auto_error=False)


def _admin_key(password: str) -> bytes:
    return hashlib.sha256(("idu-dvd-admin:" + password).encode()).digest()


def create_admin_session_token(password: str, hours: int) -> str:
    expires = str(int(time.time()) + max(1, hours) * 3600)
    signature = hmac.new(
        _admin_key(password), expires.encode(), hashlib.sha256
    ).hexdigest()
    return f"{expires}.{signature}"


def is_admin_session_authenticated(request: Request, settings: Settings) -> bool:
    password = settings.admin_password
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if not password or "." not in token:
        return False
    expires, signature = token.split(".", 1)
    if not expires.isdigit() or int(expires) < int(time.time()):
        return False
    expected = hmac.new(
        _admin_key(password), expires.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def build_service_auth(settings: Settings) -> KeycloakTokenClient:
    return KeycloakTokenClient(
        KeycloakTokenConfig(
            auth_server_url=settings.service_auth_server_url,
            realm=settings.service_auth_realm,
            client_id=settings.service_auth_client_id,
            client_secret=settings.service_auth_client_secret.get_secret_value(),
            background_refresh=True,
        )
    )


async def get_current_user_id(
    _credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    x_user_id: str | None = Header(default=None, alias=USER_ID_HEADER),
) -> str:
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{USER_ID_HEADER} header is required",
        )
    return x_user_id.strip()


async def require_service_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(optional_bearer_scheme),
) -> None:
    """Require bearer authentication, preserving the password-protected admin UI."""

    if is_admin_session_authenticated(request, app_settings):
        return
    if credentials:
        try:
            access_token = await service_token_verifier.verify_token(
                credentials.credentials
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid service token",
            ) from exc
        if access_token is not None:
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Bearer service token is required",
    )


async def get_optional_user_id(
    x_user_id: str | None = Header(default=None, alias=USER_ID_HEADER),
) -> str | None:
    return x_user_id.strip() if x_user_id and x_user_id.strip() else None


def get_mcp_user_id() -> str:
    user_id = get_http_headers(include_all=True).get("x-user-id", "").strip()
    if not user_id:
        raise ToolError(f"{USER_ID_HEADER} header is required")
    return user_id


class SyncServiceTokenAuth(httpx.Auth):
    """Bridge the async token cache to DVD's thread-pool based sync HTTP client."""

    def __init__(self, auth: KeycloakTokenClient, timeout: float) -> None:
        self.auth = auth
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, None, None]:
        if self._loop is None:
            raise RuntimeError("service auth event loop is not bound")
        future = asyncio.run_coroutine_threadsafe(
            self.auth.get_authorization_headers(), self._loop
        )
        request.headers.update(future.result(timeout=self.timeout))
        yield request


class ServiceTokenVerifier(JWTVerifier):
    """Verify Keycloak JWTs and accept only client-credentials accounts."""

    def __init__(self, settings: Settings) -> None:
        issuer = (
            f"{settings.service_auth_server_url.rstrip('/')}/realms/"
            f"{settings.service_auth_realm}"
        )
        super().__init__(
            jwks_uri=f"{issuer}/protocol/openid-connect/certs",
            issuer=issuer,
            algorithm="RS256",
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await super().verify_token(token)
        if access_token is None:
            return None
        username = access_token.claims.get("preferred_username", "")
        if not isinstance(username, str) or not username.startswith(
            SERVICE_ACCOUNT_PREFIX
        ):
            raise AuthorizationError("A service-account token is required")
        return access_token


service_token_verifier = ServiceTokenVerifier(app_settings)
