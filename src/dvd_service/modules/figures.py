"""Image normalization and deterministic document-wide noise filtering."""

from __future__ import annotations

import hashlib
import io
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import structlog
from PIL import Image

log = structlog.get_logger(__name__)
CAPTION = re.compile(r"^\s*(?:Рисунок|Рис\.|Figure)\s*\d+", re.I)


def image_block(data: bytes, *, page=None, bbox=None, max_pixels=3_500_000):
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.mode in {"RGBA", "LA"} or "transparency" in original.info:
                rgba = original.convert("RGBA")
                image = Image.new("RGB", original.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = original.convert("RGB")
            scale = min(1.0, (max_pixels / (image.width * image.height)) ** 0.5)
            if scale < 1:
                image = image.resize(
                    (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    )
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
            width, height = image.size
    except (
        OSError,
        ValueError,
        NotImplementedError,
        Image.DecompressionBombError,
    ) as exc:
        log.warning("figure_conversion_skipped", error=str(exc))
        return None
    return {
        "category": "Image",
        "text": "Рисунок без подписи",
        "html": None,
        "page": page,
        "bbox": bbox,
        "image_bytes": png,
        "figure": {
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "sha256": hashlib.sha256(png).hexdigest(),
            "page": page,
            "bbox": bbox,
            "caption": "",
            "description": "",
            "description_model": None,
            "description_generated": False,
        },
    }


def attach_captions(blocks):
    for i, block in enumerate(blocks):
        if block.get("category") != "Image" or block["figure"]["caption"]:
            continue
        neighbors = []
        for step in (1, -1):
            neighbor = i + step
            while (
                0 <= neighbor < len(blocks)
                and blocks[neighbor].get("category") == "Image"
            ):
                neighbor += step
            neighbors.append(neighbor)
        for neighbor in neighbors:
            if 0 <= neighbor < len(blocks) and CAPTION.match(blocks[neighbor]["text"]):
                block["figure"]["caption"] = blocks[neighbor]["text"]
                block["text"] = blocks[neighbor]["text"]
                break
    return blocks


def filter_figures(blocks, settings):
    images = [b for b in blocks if b.get("category") == "Image"]

    def identity(block):
        return block.get("dedup_sha256", block["figure"]["sha256"])

    occurrences = defaultdict(set)
    for block in images:
        if block.get("page") is not None:
            occurrences[identity(block)].add(block["page"])
    pages = {b.get("page") for b in blocks if b.get("page") is not None}
    repeated = {
        sha
        for sha, seen_pages in occurrences.items()
        if len(pages) >= 3 and len(seen_pages) >= max(3, len(pages) * 0.5)
    }
    seen, result = set(), []
    for block in blocks:
        if block.get("category") == "Image":
            f = block["figure"]
            sha = identity(block)
            if (
                sha in seen
                or sha in repeated
                or min(f["width"], f["height"]) < settings.figure_min_side_px
                or f["width"] * f["height"] < settings.figure_min_area_px
                or len(seen) >= settings.figure_max_per_document
            ):
                log.debug("figure_noise_skipped", sha256=sha, page=f["page"])
                continue
            seen.add(sha)
        result.append(block)
    return result


def describe_figures(raw, settings, progress, client_factory):
    """Shared content stage for ingestion and its read-only fragment preview."""
    images = [(i, b) for i, b in enumerate(raw) if b.get("category") == "Image"]
    if not images:
        return
    progress.stage("figures", len(images))
    client = client_factory(settings) if settings.vision_enabled else None

    def describe(item):
        i, block = item
        figure = block["figure"]
        description = ""
        if client is not None:
            context = "\n".join(
                b["text"]
                for b in raw[max(0, i - 1) : i + 2]
                if b.get("category") != "Image"
            )
            try:
                description = client.describe(
                    block["image_bytes"],
                    figure["sha256"],
                    figure["caption"] + "\n" + context[:4000],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "figure_description_failed",
                    sha256=figure["sha256"],
                    error=str(exc),
                )
        figure.update(
            description=description,
            description_model=client.model if description else None,
            description_generated=bool(description),
        )
        # Captionless figures remain searchable with vision disabled or unavailable.
        block["text"] = (
            "\n".join(t for t in (figure["caption"], description) if t)
            or "Рисунок без подписи"
        )

    try:
        with ThreadPoolExecutor(max(1, settings.vision_concurrency)) as pool:
            for done, _ in enumerate(pool.map(describe, images), 1):
                progress.advance(done, len(images))
    finally:
        if client:
            client.close()
    progress.complete_stage()
