"""Stage 1 + 1.5: the DocumentParser class — extraction, splitting, joining, semantic merge.

Also: content_hash (for deduplication) and preservation of table HTML.
"""

from __future__ import annotations

import hashlib
import os
import re

import structlog

from src.api_clients import OllamaClient, OllamaError
from src.common.config import Settings
from src.dvd_service.modules.windowing import make_windows, reconcile

log = structlog.get_logger(__name__)

PARSER_VERSION = "dvd-parser-2"  # 2: adds source grounding (offsets/page/bbox/span_id)

SKIP_CATEGORIES = {"Header", "Footer", "PageBreak"}

LIST_MARKER = re.compile(
    r"^\s*(\d+(?:\.\d+)*[.)]?|\w[.)]|[IVXLCDM]+[.)]|[-*•·–—‣◦])\s+\S", re.U
)
TERMINALS = (".", "!", "?", ";", ":", "…", "。", "！", "？", "»", '"', ")")
OPEN_START = ("[", "(", "«", '"')
# Do NOT split on dashes: in legal texts "–" is usually punctuation, not a list marker.
MARKER_INLINE = re.compile(
    r"(?<=\S)\s+(?=(?:\d+(?:\.\d+)+[.)]?|\d+\)|[а-яёa-z]\)|[•·‣◦])\s)", re.I | re.U
)
RU_ABBR = {
    "г",
    "гг",
    "д",
    "п",
    "пп",
    "ст",
    "рис",
    "табл",
    "см",
    "др",
    "т",
    "е",
    "руб",
    "млн",
    "млрд",
    "тыс",
    "обл",
    "респ",
    "им",
    "ул",
    "пр",
    "напр",
}
SENT_BOUND = re.compile(r'[.!?…]\s+(?=[«"(\[]?[A-ZА-ЯЁ0-9])')
# A part with its OWN number must not merge into the previous one (Stage-1.5 guard); dashes/bullets excluded.
NUMBERED_HEAD = re.compile(
    r"^\s*(\d+(?:\.\d+)+[.)]?|\d+[.)]|[IVXLCDM]+[.)]|[а-яёa-z][.)])\s+\S", re.I | re.U
)


def starts_new_marker(text: str) -> bool:
    return bool(LIST_MARKER.match(text.strip()))


def is_numbered_head(text: str) -> bool:
    return bool(NUMBERED_HEAD.match(text.strip()))


def _first_alpha_lower(text: str) -> bool:
    for ch in text.strip():
        if ch.isalpha():
            return ch.islower()
    return False


BOUNDARY_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "boundary": {"type": "string", "enum": ["new", "continuation"]},
                },
                "required": ["id", "boundary"],
            },
        }
    },
    "required": ["blocks"],
}
BOUNDARY_SYSTEM = (
    "Тебе дан пронумерованный список текстовых блоков документа по порядку. Для каждого блока "
    "по id укажи boundary: continuation - если блок является прямым продолжением предыдущего "
    "(разорванное предложение/абзац/элемент списка), либо new - если это новая самостоятельная "
    "часть. Блок 0 всегда new. Тип документа любой. Текст не меняй, верни только решения по id."
)
SEMANTIC_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "merge_with_previous": {"type": "boolean"},
                },
                "required": ["id", "merge_with_previous"],
            },
        }
    },
    "required": ["parts"],
}
SEMANTIC_MERGE_SYSTEM = (
    "Дан пронумерованный список логических частей документа по порядку. Реши, какие части "
    "нужно объединить с ПРЕДЫДУЩЕЙ в одну логическую единицу.\n"
    "merge_with_previous=true, если часть является фактическим продолжением предыдущей, "
    "её пояснением/перечислением внутри неё, либо это разрозненные малосодержательные фрагменты "
    "(титул, выходные данные, реквизиты).\n"
    "merge_with_previous=false, если часть самостоятельна. Пункт со своим номером (1.1, 4.2, "
    "а), 1)) - ВСЕГДА самостоятельная единица. id 0 всегда false. Верни решение по каждому id."
)


class DocumentParser:
    """Parses a document into logical parts (Stage 1 + 1.5)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def __repr__(self) -> str:
        s = self.settings
        return (
            f"{type(self).__name__}(split_sentences={s.split_sentences}, "
            f"sent_min_len={s.sent_min_len}, window_chars={s.window_chars}, "
            f"semantic_merge_max_passes={s.semantic_merge_max_passes})"
        )

    # --- extraction and hashing (for dedup before the heavy LLM pass) ---
    def extract_raw(self, path: str) -> list[dict]:
        # For .docx use partition_docx directly: it pulls no heavy backends (torch/OCR), which
        # segfault on some environments (Windows), and it is faster. Otherwise fall back to auto.
        if os.path.splitext(str(path))[1].lower() == ".docx":
            from unstructured.partition.docx import partition_docx

            els = partition_docx(filename=str(path))
        else:
            from unstructured.partition.auto import partition

            els = partition(
                filename=str(path),
                languages=self.settings.languages,
                strategy=self.settings.partition_strategy,
            )
        raw = []
        for el in els:
            text = (el.text or "").strip()
            if not text or el.category in SKIP_CATEGORIES:
                continue
            md = getattr(el, "metadata", None)
            html = None
            if el.category == "Table" and md is not None:
                html = getattr(md, "text_as_html", None)
            raw.append(
                {
                    "text": text,
                    "category": el.category,
                    "html": html,
                    "page": self._element_page(md),
                    "bbox": self._element_bbox(md),
                }
            )
        return raw

    @staticmethod
    def _element_page(md) -> int | None:
        """Page number from unstructured metadata (PDF/scan); None for paginationless docx."""
        return getattr(md, "page_number", None) if md is not None else None

    @staticmethod
    def _element_bbox(md) -> list[float] | None:
        """Axis-aligned [x0, y0, x1, y1] from element coordinates, when the format exposes them."""
        coords = getattr(md, "coordinates", None) if md is not None else None
        points = getattr(coords, "points", None) if coords is not None else None
        if not points:
            return None
        try:
            xs = [float(p[0]) for p in points]
            ys = [float(p[1]) for p in points]
            return [min(xs), min(ys), max(xs), max(ys)]
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def content_hash(raw: list[dict]) -> str:
        joined = "\n".join(b["text"] for b in raw)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @staticmethod
    def block_hashes(raw: list[dict]) -> list[str]:
        """Whitespace-insensitive per-block content hashes — the deterministic source-level
        fingerprint used to diff document editions (delta updates)."""
        return [
            hashlib.sha256(" ".join(b["text"].split()).encode("utf-8")).hexdigest()
            for b in raw
        ]

    @staticmethod
    def source_index(raw: list[dict]) -> tuple[str, list[dict]]:
        """Normalized source text + per-element spans, format-agnostic source grounding.

        The text is the same ``"\\n"``-join used by ``content_hash``, so each raw element maps to
        a stable ``[start, end)`` char range. Nodes later inherit offsets from the source
        elements they were built from (``src_ids``).
        """
        spans: list[dict] = []
        parts: list[str] = []
        pos = 0
        for b in raw:
            t = b["text"]
            spans.append(
                {
                    "start": pos,
                    "end": pos + len(t),
                    "page": b.get("page"),
                    "bbox": b.get("bbox"),
                }
            )
            parts.append(t)
            pos += len(t) + 1  # +1 for the "\n" join separator
        return "\n".join(parts), spans

    # --- heuristics and splitting ---
    def _heuristic_boundary(self, prev, cur, prev_cat=None, cur_cat=None) -> str:
        if cur_cat == "Table" or prev_cat == "Table":
            return "new"
        if starts_new_marker(cur):
            return "new"
        p, c = prev.strip(), cur.strip()
        if not p or not c:
            return "new"
        if (not p.endswith(TERMINALS)) and (
            _first_alpha_lower(c) or c[:1] in OPEN_START or c[:1].isdigit()
        ):
            return "continuation"
        if p[-1] in (".", "!", "?", "…") and c[:1].isupper():
            return "new"
        return "uncertain"

    def _split_sentences(self, text: str) -> list[str]:
        out, start = [], 0
        for m in SENT_BOUND.finditer(text):
            tokens = text[: m.start() + 1].split()
            last = tokens[-1].strip(".").lower() if tokens else ""
            if last in RU_ABBR or len(last) <= 1:
                continue
            out.append(text[start : m.start() + 1].strip())
            start = m.end()
        tail = text[start:].strip()
        if tail:
            out.append(tail)
        return out or [text]

    def _split_block(self, text: str) -> list[str]:
        segments = [s.strip() for s in MARKER_INLINE.split(text) if s.strip()]
        if not self.settings.split_sentences:
            return segments
        out: list[str] = []
        for seg in segments:
            out.extend(
                self._split_sentences(seg)
                if len(seg) > self.settings.sent_min_len
                else [seg]
            )
        return out

    def _split_into_segments(self, raw):
        blocks = []
        for ri, b in enumerate(raw):
            if b["category"] == "Table":
                segs = [b["text"]]
            else:
                segs = self._split_block(b["text"])
            for seg in segs:
                blocks.append(
                    {
                        "id": len(blocks),
                        "src": ri,  # index of the source raw element (for char offsets)
                        "text": seg,
                        "category": b["category"],
                        "html": b.get("html") if b["category"] == "Table" else None,
                    }
                )
        return blocks

    # --- boundary stitching (Stage 1) ---
    def _llm_boundaries(self, client: OllamaClient, window_texts):
        user = "\n".join("[%d] %s" % (i, t) for i, t in enumerate(window_texts))
        data = client.chat(BOUNDARY_SYSTEM, user, BOUNDARY_SCHEMA)
        return {item["id"]: item["boundary"] for item in data["blocks"]}

    def _assemble_boundaries(self, blocks, client, on_progress=None):
        n = len(blocks)
        heur = ["new"] + [
            self._heuristic_boundary(
                blocks[i - 1]["text"],
                blocks[i]["text"],
                blocks[i - 1]["category"],
                blocks[i]["category"],
            )
            for i in range(1, n)
        ]
        llm_dec = {}
        if client is not None:
            decisions = []
            windows = list(make_windows(blocks))
            for done, (s, e) in enumerate(windows, 1):
                texts = [blocks[k]["text"] for k in range(s, e)]
                try:
                    decisions.append((s, self._llm_boundaries(client, texts)))
                except (OllamaError, Exception) as exc:  # noqa: BLE001
                    log.warning("stage1_window_skipped", start=s, end=e, error=str(exc))
                if on_progress:
                    on_progress(done, len(windows), "boundaries")
            llm_dec = reconcile(decisions)
        final = ["new"]
        for i in range(1, n):
            final.append(heur[i] if heur[i] != "uncertain" else llm_dec.get(i, "new"))
        return final

    @staticmethod
    def _merge_blocks(blocks, boundaries):
        parts, cur = [], None
        for i, b in enumerate(blocks):
            src = b.get("src")
            if boundaries[i] == "continuation" and cur is not None:
                cur["text"] += " " + b["text"]
                cur["source_ids"].append(b["id"])
                if src is not None:
                    cur["src_ids"].append(src)
            else:
                if cur is not None:
                    parts.append(cur)
                cur = {
                    "text": b["text"],
                    "source_ids": [b["id"]],
                    "src_ids": [src] if src is not None else [],
                    "category": b["category"],
                    "html": b.get("html"),
                }
        if cur is not None:
            parts.append(cur)
        return parts

    # --- semantic merge (Stage 1.5) ---
    def _llm_semantic_merge(self, client, window_texts):
        user = "\n".join("[%d] %s" % (i, t) for i, t in enumerate(window_texts))
        data = client.chat(SEMANTIC_MERGE_SYSTEM, user, SEMANTIC_MERGE_SCHEMA)
        return {
            item["id"]: ("continuation" if item["merge_with_previous"] else "new")
            for item in data["parts"]
        }

    def _semantic_merge_pass(self, parts, client, on_progress=None, npass=1):
        decisions = []
        windows = list(make_windows(parts))
        for done, (s, e) in enumerate(windows, 1):
            texts = [parts[k]["text"] for k in range(s, e)]
            try:
                decisions.append((s, self._llm_semantic_merge(client, texts)))
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                log.warning("stage15_window_skipped", start=s, end=e, error=str(exc))
            if on_progress:
                on_progress(done, len(windows), f"semantic-merge pass {npass}")
        dec = reconcile(decisions)
        merged, cur = [], None
        for i, p in enumerate(parts):
            is_table = p.get("category") == "Table"
            prev_table = cur is not None and cur.get("category") == "Table"
            join = (
                i > 0
                and cur is not None
                and not is_table
                and not prev_table
                and not is_numbered_head(p["text"])
                and dec.get(i, "new") == "continuation"
            )
            if join:
                cur["text"] += " " + p["text"]
                cur["source_ids"] += list(p.get("source_ids", [p["id"]]))
                cur["src_ids"] += list(p.get("src_ids", []))
            else:
                if cur is not None:
                    merged.append(cur)
                cur = {
                    "text": p["text"],
                    "source_ids": list(p.get("source_ids", [p["id"]])),
                    "src_ids": list(p.get("src_ids", [])),
                    "category": p.get("category", ""),
                    "html": p.get("html"),
                }
        if cur is not None:
            merged.append(cur)
        for i, p in enumerate(merged):
            p["id"] = i
        return merged

    def semantic_merge(self, parts, client, on_progress=None):
        if client is None or len(parts) < 2:
            return parts
        for npass in range(1, self.settings.semantic_merge_max_passes + 1):
            before = len(parts)
            parts = self._semantic_merge_pass(parts, client, on_progress, npass)
            log.info("stage15_pass", npass=npass, before=before, after=len(parts))
            if len(parts) == before:
                break
        return parts

    def to_logical_parts(
        self, raw: list[dict], client: OllamaClient | None, on_progress=None
    ) -> list[dict]:
        blocks = self._split_into_segments(raw)
        log.info("stage1_split", blocks=len(raw), segments=len(blocks))
        parts = self._merge_blocks(
            blocks, self._assemble_boundaries(blocks, client, on_progress)
        )
        for i, p in enumerate(parts):
            p["id"] = i
        parts = self.semantic_merge(parts, client, on_progress)
        log.info("stage1_done", parts=len(parts))
        return parts
