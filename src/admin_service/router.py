"""Routes for the password-protected administration UI at ``/admin/ui``."""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from src.__version__ import VERSION
from src.api_clients import UrbanApiError
from src.common.auth import (
    ADMIN_SESSION_COOKIE,
    create_admin_session_token,
    is_admin_session_authenticated,
)
from src.common.config import Settings
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


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    settings: Settings = Depends(Dependencies.get_settings),
):
    if is_admin_session_authenticated(request, settings):
        return RedirectResponse("/admin/ui", status_code=303)
    configured = (
        ""
        if settings.admin_password
        else "Пароль администратора не настроен. Задайте DVD_ADMIN_PASSWORD."
    )
    return _html("login.html", error="", configured=configured)


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    password: str = Form(...),
    settings: Settings = Depends(Dependencies.get_settings),
):
    expected = settings.admin_password
    if not expected:
        return _html(
            "login.html",
            error="",
            configured="Пароль администратора не настроен. Задайте DVD_ADMIN_PASSWORD.",
        )
    if not hmac.compare_digest(password.encode(), expected.encode()):
        return _html("login.html", error="Неверный пароль", configured="")
    response = RedirectResponse("/admin/ui", status_code=303)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        create_admin_session_token(expected, settings.admin_session_hours),
        max_age=max(1, settings.admin_session_hours) * 3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


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
    settings: Settings = Depends(Dependencies.get_settings),
):
    """Urban API territory search for the panel's autocomplete (session-protected).

    A proxy rather than a client-side call: the panel's CSP allows no external host, and the
    tree has ~100k nodes, so the search has to happen server-side anyway. The parent name comes
    along because it is the only thing that tells two identically named districts apart.
    """
    if not is_admin_session_authenticated(request, settings):
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
async def admin_ui(
    request: Request,
    settings: Settings = Depends(Dependencies.get_settings),
):
    if not is_admin_session_authenticated(request, settings):
        return RedirectResponse("/admin/ui/login", status_code=303)
    return _html("admin.html", version=VERSION)
