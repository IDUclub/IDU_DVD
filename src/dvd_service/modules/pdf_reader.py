"""Read a PDF into raw blocks: the text layer where a page has one, OCR where it is a scan.

Amendments and orders of regional authorities are mostly published as scans, so a PDF is read
page by page. A page whose text layer is long enough is taken as is, one block per line (the
segmentation stages join lines into paragraphs). Any other page is rendered and sent to the OCR
service, whose layout elements map onto the block categories the pipeline already knows:
headings, list items and text become ``Title`` / ``ListItem`` / ``NarrativeText``, tables keep
their HTML, pictures become figures, running headers and footers are dropped.

OCR is slow (one page at a time on the contour server) and a job may be retried after a
restart, so every recognized page is cached on disk under the file's hash.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import structlog

from src.dvd_service.modules.figures import attach_captions, image_block

log = structlog.get_logger(__name__)

_SKIP = {"Page-header", "Page-footer"}
_CATEGORY = {
    "Title": "Title",
    "Section-header": "Title",
    "List-item": "ListItem",
    "Table": "Table",
}
_MARKDOWN = re.compile(r"^#{1,6}\s+|\*\*|__")
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_text(html: str) -> str:
    """Cell texts of an HTML table, one per line — the same shape the docx reader produces."""
    cells = (" ".join(unescape(_TAG.sub(" ", c)).split()) for c in _CELL.findall(html))
    return "\n".join(c for c in cells if c)


def layout_blocks(
    elements: list[dict],
    page: int,
    png=None,
    page_size=None,
    max_pixels=3_500_000,
) -> list[dict]:
    """Raw blocks of one OCR'd page."""
    blocks = []
    for el in elements:
        category = el.get("category") or "Text"
        text = (el.get("text") or "").strip()
        if category == "Picture":
            bbox = el.get("bbox")
            if png and isinstance(bbox, list) and len(bbox) == 4:
                from PIL import Image

                with Image.open(io.BytesIO(png)) as image:
                    x0, y0, x1, y1 = [float(v) for v in bbox]
                    clip = (
                        max(0, x0),
                        max(0, y0),
                        min(image.width, x1),
                        min(image.height, y1),
                    )
                    if clip[2] <= clip[0] or clip[3] <= clip[1]:
                        continue
                    buffer = io.BytesIO()
                    image.crop(clip).save(buffer, format="PNG")
                    # dots.ocr returns pixels of its input, figures use PDF points.
                    sx, sy = (
                        (page_size[0] / image.width, page_size[1] / image.height)
                        if page_size
                        else (1, 1)
                    )
                    figure = image_block(
                        buffer.getvalue(),
                        page=page,
                        bbox=[clip[0] * sx, clip[1] * sy, clip[2] * sx, clip[3] * sy],
                        max_pixels=max_pixels,
                    )
                    if figure:
                        blocks.append(figure)
            continue
        if category in _SKIP or not text:
            continue
        kind = _CATEGORY.get(category, "NarrativeText")
        html = None
        if kind == "Table":
            html, text = text, table_text(text) or _TAG.sub(" ", text).strip()
            if not text:
                continue
        else:
            text = unescape(_MARKDOWN.sub("", text)).strip()
        bbox = el.get("bbox")
        blocks.append(
            {
                "text": text,
                "category": kind,
                "html": html,
                "page": page,
                "bbox": [float(v) for v in bbox] if isinstance(bbox, list) else None,
            }
        )
    return attach_captions(blocks)


class PdfReader:
    """``ocr`` is a client with ``layout(png) -> elements``; without it scans are refused."""

    def __init__(
        self,
        ocr=None,
        *,
        cache_dir: str | None = None,
        dpi: int = 200,
        max_pixels: int = 3_500_000,
        min_text_chars: int = 30,
        concurrency: int = 1,
        figure_max_pixels: int = 3_500_000,
    ) -> None:
        self.ocr = ocr
        self.cache_dir = cache_dir
        self.scale = dpi / 72
        self.max_pixels = max_pixels
        self.min_text_chars = min_text_chars
        self.concurrency = max(1, concurrency)
        self.figure_max_pixels = figure_max_pixels

    def _page_figures(self, page, number):
        import pypdfium2.raw as pdfium_c

        width, height = page.get_size()
        regions = []
        clusters = []
        for obj in page.get_objects(
            filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_PATH],
            max_depth=15,
        ):
            # 4.x calls this get_pos(); 5.x renamed it to get_bounds(). Bounds
            # include the object's own matrix, but not enclosing Form matrices.
            get_bounds = getattr(obj, "get_pos", None) or obj.get_bounds
            bounds = get_bounds()
            container = getattr(obj, "container", None)
            while container is not None:
                bounds = container.get_matrix().on_rect(*bounds)
                container = getattr(container, "container", None)
            rect = self._display_bbox(page, bounds)
            if not all(math.isfinite(v) for v in rect):
                continue
            rect = [
                max(0, rect[0]),
                max(0, rect[1]),
                min(width, rect[2]),
                min(height, rect[3]),
            ]
            rw, rh = rect[2] - rect[0], rect[3] - rect[1]
            if rw <= 0 or rh <= 0:
                continue
            if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                digest = hashlib.sha256(bytes(obj.get_data())).hexdigest()
                regions.append((rect, digest))
                continue
            # Thin page rules must not connect otherwise independent diagrams.
            if min(rw, rh) <= 2 or rw * rh >= width * height * 0.95:
                continue
            while True:
                touching = [r for r in clusters if self._overlaps(r, rect, pad=8)]
                if not touching:
                    break
                for old in touching:
                    rect = [
                        min(rect[0], old[0]),
                        min(rect[1], old[1]),
                        max(rect[2], old[2]),
                        max(rect[3], old[3]),
                    ]
                    clusters.remove(old)
            clusters.append(rect)
        regions.extend(
            (r, None)
            for r in clusters
            if (
                r[2] - r[0] >= 64
                and r[3] - r[1] >= 64
                and (r[2] - r[0]) * (r[3] - r[1]) < width * height * 0.95
            )
        )
        result = []
        for rect, digest in regions:
            if any(self._overlaps(rect, b["bbox"]) for b in result):
                continue
            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            scale = min(2.0, (self.figure_max_pixels / area) ** 0.5)
            try:
                bitmap = page.render(
                    scale=scale,
                    crop=(rect[0], height - rect[3], width - rect[2], rect[1]),
                )
            except (RuntimeError, ValueError) as exc:
                log.warning(
                    "figure_render_skipped",
                    page=number,
                    bbox=rect,
                    error=str(exc),
                )
                continue
            try:
                buffer = io.BytesIO()
                bitmap.to_pil().convert("RGB").save(buffer, format="PNG")
            finally:
                bitmap.close()
            figure = image_block(
                buffer.getvalue(),
                page=number,
                bbox=rect,
                max_pixels=self.figure_max_pixels,
            )
            if figure:
                if digest:
                    figure["dedup_sha256"] = digest
                result.append(figure)
        return result

    @staticmethod
    def _overlaps(a, b, pad=0):
        return (
            a[0] < b[2] + pad
            and b[0] < a[2] + pad
            and a[1] < b[3] + pad
            and b[1] < a[3] + pad
        )

    @staticmethod
    def _display_bbox(page, bounds):
        """PDF bottom-left bounds -> top-left of the cropped, rotated page, in points.

        This has the same axes as the rendered OCR image (x right, y down), so OCR
        picture pixels scale directly into this convention. Text-only blocks retain
        their legacy bbox=None; OCR text bboxes retain their existing input pixels.
        """
        left, bottom, right, top = page.get_bbox()
        width, height = right - left, top - bottom
        rotation = page.get_rotation()
        points = []
        for x in (bounds[0], bounds[2]):
            for y in (bounds[1], bounds[3]):
                x0, y0 = x - left, y - bottom
                if rotation == 90:
                    point = (y0, x0)
                elif rotation == 180:
                    point = (width - x0, y0)
                elif rotation == 270:
                    point = (height - y0, width - x0)
                else:
                    point = (x0, height - y0)
                points.append(point)
        xs, ys = zip(*points)
        return [min(xs), min(ys), max(xs), max(ys)]

    def _interleave(self, page, blocks, images):
        textpage = page.get_textpage()
        positioned = []
        cursor = 0
        try:
            for block in blocks:
                searcher = textpage.search(block["text"], index=cursor)
                try:
                    match = searcher.get_next()
                finally:
                    searcher.close()
                if match is None:
                    return blocks + images
                start, count = match
                bounds = self._display_bbox(page, textpage.get_charbox(start))
                positioned.append((bounds[1], block))
                cursor = start + count
        except (RuntimeError, ValueError) as exc:
            log.debug("figure_text_geometry_unavailable", error=str(exc))
            return blocks + images
        finally:
            textpage.close()
        positioned.extend((b["bbox"][1], b) for b in images)
        return [b for _, b in sorted(positioned, key=lambda item: item[0])]

    @staticmethod
    def _open(path: str):
        import pypdfium2 as pdfium

        return pdfium.PdfDocument(path)

    @staticmethod
    def _page_text(page) -> str:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_range() or ""
        finally:
            textpage.close()

    def scanned_pages(self, path: str) -> list[int]:
        """1-based numbers of the pages that have no usable text layer."""
        pdf = self._open(path)
        try:
            return [
                i + 1
                for i in range(len(pdf))
                if len(self._page_text(pdf[i]).strip()) < self.min_text_chars
            ]
        finally:
            pdf.close()

    def _cached(self, key: str | None, page: int) -> list[dict] | None:
        if not key:
            return None
        try:
            with open(os.path.join(key, f"{page:05d}.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _store(self, key: str | None, page: int, elements: list[dict]) -> None:
        if not key:
            return
        os.makedirs(key, exist_ok=True)
        tmp = os.path.join(key, f"{page:05d}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(elements, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(key, f"{page:05d}.json"))

    def _recognize(self, png: bytes, key: str | None, page: int) -> list[dict]:
        elements = self.ocr.layout(png)
        self._store(key, page, elements)
        log.info("ocr_page_done", page=page, elements=len(elements))
        return elements

    def _png(self, page) -> bytes:
        width, height = page.get_size()
        scale = min(self.scale, (self.max_pixels / (width * height)) ** 0.5)
        buffer = io.BytesIO()
        bitmap = page.render(scale=scale)
        try:
            bitmap.to_pil().convert("RGB").save(buffer, format="PNG")
        finally:
            bitmap.close()
        return buffer.getvalue()

    def read(self, path: str, on_page=None) -> list[dict]:
        """Raw blocks of the whole PDF; ``on_page(done, total)`` reports OCR progress."""
        scanned = set(self.scanned_pages(path))
        if scanned and self.ocr is None:
            raise ValueError(
                "PDF содержит сканированные страницы, а OCR не настроен (DVD_OCR_BASE_URL)"
            )
        key = (
            os.path.join(self.cache_dir, file_hash(path))
            if scanned and self.cache_dir
            else None
        )
        pages: dict[int, list[dict]] = {}
        todo: list[int] = []
        pdf = self._open(path)
        try:
            for i in range(len(pdf)):
                number = i + 1
                if number not in scanned:
                    pages[number] = [
                        {
                            "text": line.strip(),
                            "category": "NarrativeText",
                            "html": None,
                            "page": number,
                            "bbox": None,
                        }
                        for line in self._page_text(pdf[i]).splitlines()
                        if line.strip()
                    ]
                    images = self._page_figures(pdf[i], number)
                    if images:
                        pages[number] = self._interleave(pdf[i], pages[number], images)
                        attach_captions(pages[number])
                elif (cached := self._cached(key, number)) is not None:
                    png = (
                        self._png(pdf[i])
                        if any(el.get("category") == "Picture" for el in cached)
                        else None
                    )
                    pages[number] = layout_blocks(
                        cached,
                        number,
                        png=png,
                        page_size=pdf[i].get_size() if png else None,
                        max_pixels=self.figure_max_pixels,
                    )
                else:
                    todo.append(number)
            if scanned:
                log.info(
                    "ocr_document", pages=len(scanned), cached=len(scanned) - len(todo)
                )
            # Pages are rendered one batch at a time: a 1500-page scan rendered up front
            # would hold gigabytes of images while the OCR server works through them.
            with ThreadPoolExecutor(self.concurrency) as pool:
                for start in range(0, len(todo), self.concurrency):
                    batch = todo[start : start + self.concurrency]
                    images = [self._png(pdf[n - 1]) for n in batch]
                    results = pool.map(
                        lambda job: self._recognize(job[0], key, job[1]),
                        zip(images, batch),
                    )
                    for number, elements, png in zip(batch, results, images):
                        pages[number] = layout_blocks(
                            elements,
                            number,
                            png=png,
                            page_size=pdf[number - 1].get_size(),
                            max_pixels=self.figure_max_pixels,
                        )
                    if on_page:
                        on_page(start + len(batch), len(todo))
        finally:
            pdf.close()
        return [block for number in sorted(pages) for block in pages[number]]
