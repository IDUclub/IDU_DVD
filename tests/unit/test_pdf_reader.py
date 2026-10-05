"""PDF reading: text layer as is, scanned pages through OCR, cached per file."""

import io
import json

import httpx
import pypdfium2 as pdfium
import pytest
from PIL import Image

from src.api_clients.ocr_client import DotsOcrClient, OcrError, parse_layout
from src.common.config import Settings
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.pdf_reader import (
    PdfReader,
    file_hash,
    layout_blocks,
    table_text,
)

TEXT = "Article 1. The rules apply to the whole territory of the town."

PAGE = [
    {"bbox": [10, 10, 500, 40], "category": "Page-header", "text": "Приложение"},
    {"bbox": [10, 50, 500, 90], "category": "Title", "text": "## Изменения в правила"},
    {"bbox": [10, 100, 500, 140], "category": "Text", "text": "1. В статье **17.1**:"},
    {"category": "Text", "text": "&lt;*&gt; - вид использования"},
    {
        "bbox": [10, 150, 500, 300],
        "category": "Table",
        "text": "<table><tr><td>7.1.</td><td>предельное количество этажей</td>"
        "<td>этаж</td><td>2</td></tr></table>",
    },
    {"bbox": [10, 310, 500, 400], "category": "Picture"},
    {"bbox": [10, 410, 500, 440], "category": "Page-footer", "text": "2"},
]


def _text_page_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, written by hand (no PDF writer needed)."""
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _mixed_pdf(tmp_path, scans: int = 1):
    """Page 1 has a text layer, the next ``scans`` pages are blank (a scan without text)."""
    text = tmp_path / "text.pdf"
    text.write_bytes(_text_page_pdf(TEXT))
    pdf = pdfium.PdfDocument(str(text))
    for _ in range(scans):
        pdf.new_page(612, 792)
    path = tmp_path / "mixed.pdf"
    pdf.save(str(path))
    pdf.close()
    return str(path)


class FakeOcr:
    def __init__(self, layout=PAGE):
        self.layout_answer = layout
        self.calls = 0

    def layout(self, png: bytes) -> list[dict]:
        assert png.startswith(b"\x89PNG")
        self.calls += 1
        return self.layout_answer


def test_layout_blocks_keep_text_tables_and_drop_page_furniture():
    blocks = layout_blocks(PAGE, 3)
    assert [b["category"] for b in blocks] == [
        "Title",
        "NarrativeText",
        "NarrativeText",
        "Table",
    ]
    assert blocks[0]["text"] == "Изменения в правила"
    assert blocks[1]["text"] == "1. В статье 17.1:"
    assert blocks[2]["text"] == "<*> - вид использования"
    assert blocks[2]["bbox"] is None
    table = blocks[3]
    assert table["html"].startswith("<table>")
    assert table["text"] == "7.1.\nпредельное количество этажей\nэтаж\n2"
    assert all(b["page"] == 3 for b in blocks)
    assert blocks[0]["bbox"] == [10.0, 50.0, 500.0, 90.0]


def test_table_text_unescapes_and_skips_empty_cells():
    assert table_text("<tr><td>a &amp; b</td><td> </td><th>c</th></tr>") == "a & b\nc"


def test_parse_layout_accepts_fences_wrappers_and_plain_text():
    assert parse_layout('```json\n[{"category": "Text", "text": "a"}]\n```') == [
        {"category": "Text", "text": "a"}
    ]
    assert parse_layout('{"layout": [{"category": "Title", "text": "b"}]}') == [
        {"category": "Title", "text": "b"}
    ]
    # A broken answer still carries the page text: it is kept, not dropped.
    assert parse_layout("Статья 1. Текст") == [
        {"category": "Text", "text": "Статья 1. Текст"}
    ]
    assert parse_layout("") == []
    # An answer cut off by the token limit keeps the elements that were complete.
    cut = (
        '[{"category": "Text", "text": "a"}, {"category": "Table", "text": "<table><tr>'
    )
    assert parse_layout(cut) == [{"category": "Text", "text": "a"}]


def test_text_layer_pages_are_read_without_ocr(tmp_path):
    path = tmp_path / "text.pdf"
    path.write_bytes(_text_page_pdf(TEXT))
    ocr = FakeOcr()
    blocks = PdfReader(ocr).read(str(path))
    assert [b["text"] for b in blocks] == [TEXT]
    assert blocks[0]["page"] == 1
    assert ocr.calls == 0


def test_scanned_pages_go_to_ocr_in_page_order_and_are_cached(tmp_path):
    path = _mixed_pdf(tmp_path, scans=2)
    ocr = FakeOcr()
    progress = []
    reader = PdfReader(ocr, cache_dir=str(tmp_path / "cache"))
    blocks = reader.read(
        path, on_page=lambda done, total: progress.append((done, total))
    )
    assert [b["page"] for b in blocks] == [1, 2, 2, 2, 2, 3, 3, 3, 3]
    assert blocks[0]["text"] == TEXT
    assert ocr.calls == 2
    assert progress == [(1, 2), (2, 2)]
    # A retried job reads the recognized pages back instead of calling OCR again.
    again = PdfReader(FakeOcr(layout=[]), cache_dir=str(tmp_path / "cache"))
    assert again.read(path) == blocks
    assert again.ocr.calls == 0


def test_large_pages_are_rendered_within_the_pixel_budget():
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(1190, 842)  # A3 landscape: 3308 x 2339 at 200 dpi
    png = PdfReader(FakeOcr(), dpi=200, max_pixels=1_000_000)._png(pdf[0])
    width, height = Image.open(io.BytesIO(png)).size
    assert width * height <= 1_000_000 * 1.01  # rendering rounds sides up
    assert width > height


def test_scans_without_ocr_are_refused(tmp_path):
    with pytest.raises(ValueError, match="OCR не настроен"):
        PdfReader(None).read(_mixed_pdf(tmp_path))


def test_upload_hash_of_a_scan_is_its_bytes_and_runs_no_ocr(tmp_path):
    path = _mixed_pdf(tmp_path)
    ocr = FakeOcr()
    parser = DocumentParser(Settings(ocr_cache_dir=str(tmp_path / "c")), ocr=ocr)
    assert parser.upload_hash(path) == file_hash(path)
    assert ocr.calls == 0
    with pytest.raises(ValueError, match="OCR не настроен"):
        DocumentParser(Settings()).upload_hash(path)


def test_upload_hash_of_a_text_pdf_is_its_text(tmp_path):
    path = tmp_path / "text.pdf"
    path.write_bytes(_text_page_pdf(TEXT))
    parser = DocumentParser(Settings())
    assert parser.upload_hash(str(path)) == parser.content_hash(
        parser.extract_raw(str(path))
    )


def _client(handler, **kwargs):
    client = DotsOcrClient(
        "http://ocr:8010",
        api_key="k",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    client.backoff = 0
    return client


def test_ocr_client_sends_the_page_and_reads_the_layout():
    seen = {}

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "dots.ocr"}]})
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        answer = json.dumps([{"category": "Text", "text": "a"}])
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    assert _client(handler).layout(b"\x89PNG") == [{"category": "Text", "text": "a"}]
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "dots.ocr"
    # The answer may take whatever context the image leaves: no fixed max_tokens.
    assert "max_tokens" not in seen["body"]
    image = seen["body"]["messages"][0]["content"][0]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,")


def test_ocr_client_retries_server_errors_but_not_client_errors():
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    assert _client(flaky, model="m").layout(b"x") == []
    assert len(calls) == 2

    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda r: httpx.Response(401), model="m").layout(b"x")

    with pytest.raises(OcrError):
        _client(lambda r: httpx.Response(500), model="m", max_retries=2).layout(b"x")
