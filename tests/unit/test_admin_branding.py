"""Logo processing and HTTP persistence without external services."""

import base64
import io
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.server.auth import AccessToken
from minio.error import S3Error
from PIL import Image, ImageDraw

from src.admin_service.branding import BrandingService
from src.admin_service.router import router
from src.common import auth
from src.dependencies import Dependencies


def encoded(image, format="PNG"):
    output = io.BytesIO()
    image.save(output, format=format)
    return output.getvalue()


def logo(background="white", format="PNG"):
    image = Image.new("RGB", (80, 60), background)
    ImageDraw.Draw(image).rectangle((20, 15, 60, 45), fill="red")
    return encoded(image, format)


@pytest.mark.parametrize("background", ["white", "black", "#245578"])
@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_removes_border_background_and_preserves_mark(background, format):
    result = BrandingService(Mock()).prepare(logo(background, format))
    image = Image.open(io.BytesIO(result))
    assert image.mode == "RGBA"
    assert image.size == (512, 512)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((256, 256))[0] > 200
    assert image.getpixel((256, 256))[3] == 255


def test_preserves_existing_alpha_and_interior_white():
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 5, 35, 35), fill=(255, 255, 255, 128))
    result = Image.open(io.BytesIO(BrandingService(Mock()).prepare(encoded(image))))
    assert result.getpixel((256, 256)) == (255, 255, 255, 128)


def test_does_not_erase_enclosed_foreground_of_background_color():
    image = Image.new("RGB", (60, 60), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 50, 50), fill="black")
    draw.rectangle((20, 20, 40, 40), fill="white")
    result = Image.open(io.BytesIO(BrandingService(Mock()).prepare(encoded(image))))
    assert result.getpixel((256, 256)) == (255, 255, 255, 255)


@pytest.mark.parametrize(
    "data",
    [b"", b"bad image", b"<svg></svg>", encoded(Image.new("RGB", (8, 8), "white"))],
)
def test_invalid_or_empty_image_rejected(data):
    with pytest.raises(ValueError):
        BrandingService(Mock()).prepare(data)


def test_pixel_limit_checked_before_decode(monkeypatch):
    monkeypatch.setattr(BrandingService, "MAX_PIXELS", 100)
    with pytest.raises(ValueError, match="16 млн"):
        BrandingService(Mock()).prepare(logo())


def test_bundled_logo_has_real_transparency():
    path = Path("src/admin_service/static/logo.png")
    with Image.open(path) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((image.width // 2, image.height // 2))[3] == 0
        assert image.getchannel("A").getextrema() == (0, 255)


@pytest.fixture
def client(monkeypatch):
    storage = Mock()
    storage.download.side_effect = S3Error(
        None, "NoSuchKey", "missing", "logo", "request", "host"
    )
    service = BrandingService(storage)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[Dependencies.get_branding] = lambda: service

    async def verify(token):
        if token not in {"admin", "user"}:
            return None
        return AccessToken(
            token=token,
            client_id="ui",
            scopes=[],
            claims={
                "preferred_username": token,
                "realm_access": {"roles": ["ADMIN"] if token == "admin" else []},
            },
        )

    monkeypatch.setattr(auth.keycloak_token_verifier, "verify_token", verify)
    with TestClient(app) as http:
        yield http, storage


@pytest.mark.parametrize("token,status", [(None, 401), ("invalid", 401), ("user", 403)])
def test_upload_and_preview_require_admin(client, token, status):
    http, storage = client
    for suffix in ("", "?preview=true"):
        response = http.post(
            "/admin/ui/logo" + suffix,
            files={"file": ("logo.png", logo())},
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        assert response.status_code == status
    storage.upload.assert_not_called()


def test_preview_then_save_is_persistent_and_public(client):
    http, storage = client
    http.cookies.set(auth.ADMIN_SESSION_COOKIE, "admin")
    upload = {"file": ("logo.jpg", logo(format="JPEG"), "image/jpeg")}
    preview = http.post("/admin/ui/logo?preview=true", files=upload)
    assert preview.status_code == 200
    expected = base64.b64decode(preview.json()["preview"].split(",")[1])
    storage.upload.assert_not_called()
    assert http.post("/admin/ui/logo", files=upload).status_code == 200
    storage.upload.assert_called_once_with(BrandingService.KEY, expected, "image/png")
    storage.download.side_effect = None
    storage.download.return_value = (expected, "image/png")
    http.cookies.clear()
    assert http.get("/admin/ui/logo.png").content == expected
    icon = http.get("/admin/ui/favicon.png")
    assert Image.open(io.BytesIO(icon.content)).size == (64, 64)
    assert icon.headers["cache-control"] == "no-store"
    assert icon.headers["x-content-type-options"] == "nosniff"


def test_default_logo_and_storage_failure(client):
    http, storage = client
    assert (
        http.get("/admin/ui/logo.png").content
        == Path("src/admin_service/static/logo.png").read_bytes()
    )
    storage.download.side_effect = RuntimeError("unavailable")
    assert http.get("/admin/ui/logo.png").status_code == 503
    storage.upload.side_effect = RuntimeError("unavailable")
    http.cookies.set(auth.ADMIN_SESSION_COOKIE, "admin")
    assert (
        http.post("/admin/ui/logo", files={"file": ("logo.png", logo())}).status_code
        == 503
    )


def test_invalid_and_oversized_upload_does_not_overwrite(client, monkeypatch):
    http, storage = client
    http.cookies.set(auth.ADMIN_SESSION_COOKIE, "admin")
    assert (
        http.post(
            "/admin/ui/logo", files={"file": ("logo.png", b"invalid")}
        ).status_code
        == 400
    )
    monkeypatch.setattr(BrandingService, "MAX_BYTES", 10)
    assert (
        http.post("/admin/ui/logo", files={"file": ("logo.png", b"x" * 11)}).status_code
        == 413
    )
    storage.upload.assert_not_called()
