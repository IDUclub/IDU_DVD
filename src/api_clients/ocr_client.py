"""Client for dots.ocr served by vLLM: one page image in, its layout elements out.

The model answers the layout prompt with a JSON array of elements in reading order, each
``{"bbox": [x1, y1, x2, y2], "category": ..., "text": ...}``: tables come as HTML, formulas as
LaTeX, everything else as Markdown, pictures without text. The server is OpenAI-compatible
(``/v1/chat/completions`` with an ``image_url`` part) and takes one page per request.
"""

from __future__ import annotations

import base64
import json
import re
import time

import httpx
import structlog

log = structlog.get_logger(__name__)

LAYOUT_PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others: Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class OcrError(RuntimeError):
    """The OCR service failed or answered something that is not a page layout."""


def parse_layout(answer: str) -> list[dict]:
    """The layout elements of one answer; a non-JSON answer is kept as one text element.

    A model that runs out of tokens or ignores the format still read the page: dropping its
    text would lose the page silently, so the answer is kept as plain text instead.
    """
    body = _FENCE.sub("", answer or "").strip()
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = _complete_elements(body)
        if data is None:
            log.warning("ocr_layout_not_json", chars=len(body))
            return [{"category": "Text", "text": body}]
        log.warning("ocr_layout_truncated", elements=len(data))
    if isinstance(data, dict):
        data = data.get("layout") or data.get("elements") or [data]
    return [el for el in data if isinstance(el, dict)]


def _complete_elements(body: str) -> list | None:
    """The complete elements of an array cut off mid-element (the answer ran out of tokens)."""
    if not body.startswith("["):
        return None
    end = body.rfind("}")
    while end > 0:
        try:
            return json.loads(body[: end + 1] + "]")
        except json.JSONDecodeError:
            end = body.rfind("}", 0, end)
    return None


class DotsOcrClient:
    """Synchronous, like the other pipeline clients: ingestion runs in a worker thread."""

    def __init__(
        self,
        base_url: str,
        model: str = "",
        api_key: str | None = None,
        timeout: float = 300.0,
        max_retries: int = 3,
        max_tokens: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        if not self.base.endswith("/v1"):
            self.base += "/v1"
        self.model = model
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.backoff = 1.0
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            timeout=timeout, headers=headers, transport=transport
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base={self.base}, model={self.model or 'auto'})"

    def close(self) -> None:
        self._client.close()

    def _model(self) -> str:
        if not self.model:
            resp = self._client.get(self.base + "/models")
            resp.raise_for_status()
            models = resp.json().get("data") or []
            if not models:
                raise OcrError("OCR server lists no models")
            self.model = models[0]["id"]
        return self.model

    def layout(self, png: bytes) -> list[dict]:
        """Layout elements of one page image (PNG bytes), in reading order."""
        image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        body = {
            "model": self._model(),
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image}},
                        {"type": "text", "text": LAYOUT_PROMPT},
                    ],
                }
            ],
        }
        # Without max_tokens vLLM lets the answer take whatever the context leaves after the
        # image; a fixed value larger than that (dots.mocr serves 8192 tokens) is a 400.
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.post(self.base + "/chat/completions", json=body)
                if resp.status_code < 500:
                    resp.raise_for_status()
                    answer = resp.json()["choices"][0]["message"]["content"]
                    return parse_layout(answer)
                last = OcrError(f"OCR server error {resp.status_code}")
            except httpx.TransportError as exc:
                last = exc
            # A busy single-page server sheds load with 5xx/timeouts: back off and retry.
            log.warning("ocr_retry", attempt=attempt + 1, error=str(last))
            time.sleep(self.backoff * 2**attempt)
        raise OcrError(f"OCR failed after {self.max_retries} attempts: {last}")
