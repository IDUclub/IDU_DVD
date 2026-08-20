from __future__ import annotations

import asyncio
import time
from collections.abc import Generator

import httpx
from fastapi import Header, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_http_headers
from idu_service_auth import KeycloakTokenClient, KeycloakTokenConfig

from src.common.config import Settings
from src.common.config import settings as app_settings

USER_ID_HEADER = "X-User-Id"
SERVICE_ACCOUNT_PREFIX = "service-account-"
ADMIN_SESSION_COOKIE = "dvd_admin_session"
REQUEST_TOKEN_ATTR = "dvd_access_token"
bearer_scheme = HTTPBearer(auto_error=True)
optional_bearer_scheme = HTTPBearer(auto_error=False)

_MISSING = object()


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


def _is_service_account(access_token: AccessToken) -> bool:
    username = access_token.claims.get("preferred_username", "")
    return isinstance(username, str) and username.startswith(SERVICE_ACCOUNT_PREFIX)


def _reject_expired(access_token: AccessToken) -> AccessToken:
    """Refuse a token that is past its ``exp``, whatever the verifier made of it."""

    expires_at = access_token.expires_at
    if expires_at is not None and int(expires_at) <= int(time.time()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token has expired",
        )
    return access_token


async def _verify_bearer(
    token: str, verifier: TokenVerifier, detail: str
) -> AccessToken:
    """Verify signature, issuer and freshness, or answer 401 with ``detail``."""

    try:
        access_token = await verifier.verify_token(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
        ) from exc
    if access_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
        )
    return _reject_expired(access_token)


def _presented_token(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str | None:
    """The caller's token, from the Authorization header or the admin panel's cookie.

    The panel stores the very token the auth helper issued, so a browser session is not a
    second kind of credential — it is the same bearer token arriving by another route.
    """

    if credentials:
        return credentials.credentials
    return request.cookies.get(ADMIN_SESSION_COOKIE) or None


async def _authenticate(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> AccessToken:
    """Authenticate the request once and cache the result on ``request.state``."""

    cached = getattr(request.state, REQUEST_TOKEN_ATTR, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]

    token = _presented_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is required",
        )
    access_token = await _verify_bearer(
        token, keycloak_token_verifier, "Invalid bearer token"
    )
    setattr(request.state, REQUEST_TOKEN_ATTR, access_token)
    return access_token


async def require_authenticated(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(optional_bearer_scheme),
) -> None:
    """Require any live Keycloak token — a user's, a service's, or the panel's session.

    Read-only access to the shared corpus is gated on this; changing it needs
    :func:`require_admin`.
    """

    await _authenticate(request, credentials)


def _realm_roles(access_token: AccessToken) -> set[str]:
    realm_access = access_token.claims.get("realm_access")
    roles = realm_access.get("roles") if isinstance(realm_access, dict) else None
    if not isinstance(roles, list):
        return set()
    return {role for role in roles if isinstance(role, str)}


def assert_admin(access_token: AccessToken) -> AccessToken:
    """Entitle the token to change the shared corpus, or refuse it with 403.

    A service account passes on its client credentials alone; a person has to carry the
    ``DVD_ADMIN_ROLE`` realm role. 403 rather than 401 is the honest answer here — the token
    is valid, the human behind it simply is not entitled, and the two need different fixes.
    """

    if _is_service_account(access_token):
        return access_token
    role = app_settings.admin_role
    if role not in _realm_roles(access_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"realm role {role} is required",
        )
    return access_token


async def verify_admin_token(token: str) -> AccessToken:
    """Verify a freshly issued token and check it entitles its holder to the panel."""

    return assert_admin(
        await _verify_bearer(token, keycloak_token_verifier, "Invalid bearer token")
    )


async def admin_session(request: Request) -> AccessToken | None:
    """The admin behind the panel's session cookie, or ``None`` if there is not one.

    Used by the server-rendered pages, which redirect to the login form rather than answer
    401 — an expired token and a revoked role both land the visitor back at the login page.
    """

    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not token:
        return None
    try:
        return await verify_admin_token(token)
    except HTTPException:
        return None


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(optional_bearer_scheme),
) -> None:
    """Guard the shared corpus — and the service's own controls — against mere visitors."""

    assert_admin(await _authenticate(request, credentials))


def _subject_of(access_token: AccessToken) -> str:
    user_id = access_token.claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token does not contain user id",
        )
    return user_id.strip()


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    x_user_id: str | None = Header(default=None, alias=USER_ID_HEADER),
) -> str:
    """The user the caller acts as — mandatory, for endpoints that only touch user data."""

    access_token = await _verify_bearer(
        credentials.credentials, keycloak_token_verifier, "Invalid bearer token"
    )
    if _is_service_account(access_token):
        if x_user_id and x_user_id.strip():
            return x_user_id.strip()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{USER_ID_HEADER} header is required",
        )
    return _subject_of(access_token)


async def get_effective_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(optional_bearer_scheme),
    x_user_id: str | None = Header(default=None, alias=USER_ID_HEADER),
) -> str | None:
    """The user whose private index the caller may reach, or ``None`` for shared-only access.

    A user token owns itself: its ``sub`` decides and ``X-User-Id`` is ignored, so no
    authenticated user can aim a search at somebody else's index. Only a service account may
    act on behalf of a user, and only by naming them in ``X-User-Id``.
    """

    access_token = await _authenticate(request, credentials)
    if _is_service_account(access_token):
        return x_user_id.strip() if x_user_id and x_user_id.strip() else None
    return _subject_of(access_token)


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


class ServiceTokenVerifier(TokenVerifier):
    """Verify Keycloak JWTs and accept only client-credentials accounts."""

    def __init__(self, verifier: JWTVerifier) -> None:
        super().__init__()
        self.verifier = verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await self.verifier.verify_token(token)
        if access_token is None:
            return None
        if not _is_service_account(access_token):
            raise AuthorizationError("A service-account token is required")
        return access_token


_issuer = (
    f"{app_settings.service_auth_server_url.rstrip('/')}/realms/"
    f"{app_settings.service_auth_realm}"
)
keycloak_token_verifier = JWTVerifier(
    jwks_uri=f"{_issuer}/protocol/openid-connect/certs",
    issuer=_issuer,
    algorithm="RS256",
)
service_token_verifier = ServiceTokenVerifier(keycloak_token_verifier)
