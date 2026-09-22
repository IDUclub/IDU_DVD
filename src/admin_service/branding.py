"""Validated, transparent admin branding, persisted as a single MinIO object."""

from __future__ import annotations

import io
from pathlib import Path
from statistics import median

from minio.error import S3Error
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from src.common.db.minio_client import DocumentStorage


class BrandingService:
    MAX_BYTES = 5 * 1024 * 1024
    MAX_PIXELS = 16_000_000
    KEY = "_admin/branding/logo.png"

    def __init__(self, storage: DocumentStorage) -> None:
        self.storage = storage

    @staticmethod
    def _png(image: Image.Image) -> bytes:
        output = io.BytesIO()
        # Do not retain user-supplied metadata in the public asset.
        image.info.clear()
        image.save(output, format="PNG")
        return output.getvalue()

    def prepare(self, data: bytes) -> bytes:
        if not data or len(data) > self.MAX_BYTES:
            raise ValueError("Выберите изображение размером до 5 МБ.")
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("Поддерживаются PNG, JPEG и WebP.")
                if source.width * source.height > self.MAX_PIXELS:
                    raise ValueError(
                        "Изображение должно содержать не более 16 млн пикселей."
                    )
                if getattr(source, "n_frames", 1) != 1:
                    raise ValueError("Выберите изображение без анимации.")
                image = ImageOps.exif_transpose(source).convert("RGBA")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Не удалось прочитать изображение.") from exc
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        if image.getchannel("A").getextrema() == (255, 255):
            self._remove_background(image)
        bounds = image.getchannel("A").getbbox()
        if bounds is None:
            raise ValueError("После удаления фона изображение пустое.")
        image = image.crop(bounds)
        image.thumbnail((480, 480), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (512, 512))
        canvas.paste(image, ((512 - image.width) // 2, (512 - image.height) // 2))
        return self._png(canvas)

    @staticmethod
    def _remove_background(image: Image.Image) -> None:
        """Remove only near-uniform background connected to the image perimeter."""
        width, height = image.size
        edges = [image.getpixel((x, y)) for x in range(width) for y in (0, height - 1)]
        edges += [image.getpixel((x, y)) for y in range(height) for x in (0, width - 1)]
        background = tuple(int(median(p[c] for p in edges)) for c in range(3))

        def distance(pixel):
            return max(abs(pixel[c] - background[c]) for c in range(3))

        if sum(distance(p) <= 35 for p in edges) < len(edges) * 0.75:
            raise ValueError(
                "Не найден однотонный фон. Используйте PNG с прозрачным фоном."
            )
        # Padding connects all perimeter background regions in one flood fill.
        mask = Image.new("L", (width + 2, height + 2), 0)
        candidates = Image.new("L", image.size)
        candidates.putdata([0 if distance(p) <= 35 else 255 for p in image.getdata()])
        mask.paste(candidates, (1, 1))
        ImageDraw.floodfill(mask, (0, 0), 128)
        alpha = mask.crop((1, 1, width + 1, height + 1)).point(
            lambda value: 0 if value == 128 else 255
        )
        image.putalpha(alpha)

    def save(self, data: bytes) -> None:
        self.storage.upload(self.KEY, self.prepare(data), "image/png")

    def read(self, *, favicon: bool = False) -> bytes:
        try:
            data, _ = self.storage.download(self.KEY)
        except S3Error as exc:
            if exc.code != "NoSuchKey":
                raise
            data = (Path(__file__).parent / "static" / "logo.png").read_bytes()
        if not favicon:
            return data
        with Image.open(io.BytesIO(data)) as image:
            icon = ImageOps.pad(image.convert("RGBA"), (64, 64), color=(0, 0, 0, 0))
            return self._png(icon)
