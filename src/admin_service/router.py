"""Routes for the administration UI at ``/admin/ui``.

Entry is a Keycloak login: the form posts to the IDU auth helper through
:class:`AuthHelperClient`, and only a user carrying the ``DVD_ADMIN_ROLE`` realm role is let
in. The session cookie holds the issued access token itself, so the panel's own API calls are
authenticated exactly like any other bearer request and expire with the token.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from src.__version__ import VERSION
from src.api_clients import AuthHelperClient, AuthHelperError, UrbanApiError
from src.common.auth import ADMIN_SESSION_COOKIE, admin_session, verify_admin_token
from src.dependencies import Dependencies

router = APIRouter(prefix="/admin/ui", tags=["admin-ui"], include_in_schema=False)

_ROOT = Path(__file__).resolve().parent
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)


def _html(name: str, **values: str) -> HTMLResponse:
    content = (_ROOT / "templates" / name).read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{ " + key + " }}", value)
    return HTMLResponse(
        content,
        headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store"},
    )


_NOT_CONFIGURED = (
    "Вход не настроен. Задайте DVD_AUTH_HELPER_URL и DVD_AUTH_HELPER_API_KEY."
)
_NOT_ENTITLED = "У этой учётной записи нет прав администратора."
_SESSION_FALLBACK_SECONDS = 3600


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    auth_helper: AuthHelperClient = Depends(Dependencies.get_auth_helper),
):
    if await admin_session(request):
        return RedirectResponse("/admin/ui", status_code=303)
    return _html(
        "login.html",
        error="",
        configured="" if auth_helper.configured else _NOT_CONFIGURED,
    )


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    auth_helper: AuthHelperClient = Depends(Dependencies.get_auth_helper),
):
    """Log in with Keycloak credentials and keep the issued token as the session."""

    try:
        token = await auth_helper.issue_token(username, password)
    except AuthHelperError as exc:
        if exc.status_code == 503:
            return _html("login.html", error="", configured=_NOT_CONFIGURED)
        error = (
            "Неверный логин или пароль"
            if exc.status_code == 401
            else "Сервис авторизации недоступен, попробуйте позже"
        )
        return _html("login.html", error=error, configured="")

    try:
        access_token = await verify_admin_token(token)
    except HTTPException as exc:
        error = _NOT_ENTITLED if exc.status_code == 403 else "Токен не принят"
        return _html("login.html", error=error, configured="")

    response = RedirectResponse("/admin/ui", status_code=303)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        token,
        max_age=_session_seconds(access_token.expires_at),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


def _session_seconds(expires_at: int | None) -> int:
    """Outlive the token by nothing: the cookie is useless the moment it stops verifying."""

    if expires_at is None:
        return _SESSION_FALLBACK_SECONDS
    return max(60, int(expires_at) - int(time.time()))


@router.post("/logout")
async def logout():
    response = RedirectResponse("/admin/ui/login", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    return response


@router.get("/territories")
async def territories(
    request: Request,
    query: str = "",
    limit: int = 20,
):
    """Urban API territory search for the panel's autocomplete (session-protected).

    A proxy rather than a client-side call: the panel's CSP allows no external host, and the
    tree has ~100k nodes, so the search has to happen server-side anyway. The parent name comes
    along because it is the only thing that tells two identically named districts apart.
    """
    if not await admin_session(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    try:
        found = await run_in_threadpool(
            Dependencies.get_urban_api().find_by_name, query, limit=limit
        )
    except UrbanApiError as exc:
        return JSONResponse({"detail": f"Urban API недоступен: {exc}"}, status_code=502)
    return {
        "count": len(found),
        "territories": [
            {
                "territory_id": territory.territory_id,
                "name": territory.name,
                "parent_name": territory.parent_name,
                "type_name": territory.type_name,
                "document_level": territory.document_level,
            }
            for territory in found
        ],
    }


@router.get("/assets/{filename}")
async def asset(filename: str):
    allowed = {"admin.css": "text/css", "admin.js": "application/javascript"}
    if filename not in allowed:
        return Response(status_code=404)
    return Response(
        (_ROOT / "static" / filename).read_text(encoding="utf-8"),
        media_type=allowed[filename],
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("")
@router.get("/")
async def admin_ui(request: Request):
    if not await admin_session(request):
        return RedirectResponse("/admin/ui/login", status_code=303)
    return _html("admin.html", version=VERSION)
