"""Read a PDF into raw blocks: the text layer where a page has one, OCR where it is a scan.

Amendments and orders of regional authorities are mostly published as scans, so a PDF is read
page by page. A page whose text layer is long enough is taken as is, one block per line (the
segmentation stages join lines into paragraphs). Any other page is rendered and sent to the OCR
service, whose layout elements map onto the block categories the pipeline already knows:
headings, list items and text become ``Title`` / ``ListItem`` / ``NarrativeText``, tables keep
their HTML, running headers, footers and pictures are dropped.

OCR is slow (one page at a time on the contour server) and a job may be retried after a
restart, so every recognized page is cached on disk under the file's hash.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import structlog

log = structlog.get_logger(__name__)

_SKIP = {"Page-header", "Page-footer", "Picture"}
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


def layout_blocks(elements: list[dict], page: int) -> list[dict]:
    """Raw blocks of one OCR'd page."""
    blocks = []
    for el in elements:
        category = el.get("category") or "Text"
        text = (el.get("text") or "").strip()
        if category in _SKIP or not text:
            continue
        kind = _CATEGORY.get(category, "NarrativeText")
        html = None
        if kind == "Table":
            html, text = text, table_text(text) or _TAG.sub(" ", text).strip()
            if not text:
                continue
        else:
            text = _MARKDOWN.sub("", text).strip()
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
    return blocks


class PdfReader:
    """``ocr`` is a client with ``layout(png) -> elements``; without it scans are refused."""

    def __init__(
        self,
        ocr=None,
        *,
        cache_dir: str | None = None,
        dpi: int = 200,
        min_text_chars: int = 30,
        concurrency: int = 1,
    ) -> None:
        self.ocr = ocr
        self.cache_dir = cache_dir
        self.scale = dpi / 72
        self.min_text_chars = min_text_chars
        self.concurrency = max(1, concurrency)

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
        buffer = io.BytesIO()
        page.render(scale=self.scale).to_pil().convert("RGB").save(buffer, format="PNG")
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
                elif (cached := self._cached(key, number)) is not None:
                    pages[number] = layout_blocks(cached, number)
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
                    for number, elements in zip(batch, results):
                        pages[number] = layout_blocks(elements, number)
                    if on_page:
                        on_page(start + len(batch), len(todo))
        finally:
            pdf.close()
        return [block for number in sorted(pages) for block in pages[number]]
