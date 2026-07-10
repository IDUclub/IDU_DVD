"""Routes for the password-protected administration UI at ``/admin/ui``."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.__version__ import VERSION
from src.common.config import Settings
from src.dependencies import Dependencies

router = APIRouter(prefix="/admin/ui", tags=["admin-ui"], include_in_schema=False)

_COOKIE = "dvd_admin_session"
_ROOT = Path(__file__).resolve().parent
_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)


def _key(password: str) -> bytes:
    return hashlib.sha256(("idu-dvd-admin:" + password).encode()).digest()


def _token(password: str, hours: int) -> str:
    expires = str(int(time.time()) + max(1, hours) * 3600)
    signature = hmac.new(_key(password), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def _authenticated(request: Request, settings: Settings) -> bool:
    password = settings.admin_password
    token = request.cookies.get(_COOKIE, "")
    if not password or "." not in token:
        return False
    expires, signature = token.split(".", 1)
    if not expires.isdigit() or int(expires) < int(time.time()):
        return False
    expected = hmac.new(_key(password), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


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
    if _authenticated(request, settings):
        return RedirectResponse("/admin/ui", status_code=303)
    configured = "" if settings.admin_password else "Пароль администратора не настроен. Задайте DVD_ADMIN_PASSWORD."
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
        _COOKIE,
        _token(expected, settings.admin_session_hours),
        max_age=max(1, settings.admin_session_hours) * 3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/admin/ui",
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/admin/ui/login", status_code=303)
    response.delete_cookie(_COOKIE, path="/admin/ui")
    return response


@router.get("/assets/{filename}")
async def asset(filename: str):
    allowed = {"admin.css": "text/css", "admin.js": "application/javascript"}
    if filename not in allowed:
        return Response(status_code=404)
    return Response(
        (_ROOT / "static" / filename).read_text(encoding="utf-8"),
        media_type=allowed[filename],
        headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


@router.get("")
@router.get("/")
async def admin_ui(
    request: Request,
    settings: Settings = Depends(Dependencies.get_settings),
):
    if not _authenticated(request, settings):
        return RedirectResponse("/admin/ui/login", status_code=303)
    return _html("admin.html", version=VERSION)
