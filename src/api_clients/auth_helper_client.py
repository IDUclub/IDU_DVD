"""Client for the IDU auth helper — the browser's way in to the admin panel.

Mirrors gMART's ``POST /auth/token`` proxy so both front doors log in against the same
service: the browser posts a username and password, the server attaches the helper's API key
and forwards them, and a Keycloak access token comes back. The key never reaches the browser.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)

API_KEY_HEADER = "X-Auth-Helper-Api-Key"


class AuthHelperError(RuntimeError):
    """The helper refused the credentials, was not configured, or could not be reached."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthHelperClient:
    """Exchange Keycloak credentials for an access token through the IDU auth helper."""

    SCOPE = "openid profile email"

    def __init__(
        self, base: str | None, api_key: str | None, timeout: float = 15.0
    ) -> None:
        self.base = (base or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        """Both halves are needed: an address to ask and a key it will accept."""

        return bool(self.base and self.api_key)

    async def issue_token(self, username: str, password: str) -> str:
        if not self.configured:
            raise AuthHelperError(
                "auth helper is not configured — set DVD_AUTH_HELPER_URL and "
                "DVD_AUTH_HELPER_API_KEY",
                status_code=503,
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base}/api/token",
                    headers={API_KEY_HEADER: self.api_key},
                    json={
                        "username": username,
                        "password": password,
                        "scope": self.SCOPE,
                    },
                )
        except httpx.HTTPError as exc:
            log.warning("auth_helper_unreachable", error=str(exc))
            raise AuthHelperError(f"auth helper is unreachable: {exc}") from exc

        if response.status_code in (400, 401, 403):
            raise AuthHelperError("invalid username or password", status_code=401)
        if response.status_code >= 400:
            log.warning("auth_helper_error", status=response.status_code)
            raise AuthHelperError(f"auth helper answered {response.status_code}")

        try:
            token = (response.json() or {}).get("access_token")
        except ValueError as exc:
            raise AuthHelperError("auth helper returned a malformed body") from exc
        if not isinstance(token, str) or not token:
            raise AuthHelperError("auth helper returned no access_token")
        return token
