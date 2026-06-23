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
            html = None
            if el.category == "Table" and getattr(el, "metadata", None) is not None:
                html = getattr(el.metadata, "text_as_html", None)
            raw.append({"text": text, "category": el.category, "html": html})
        return raw

    @staticmethod
    def content_hash(raw: list[dict]) -> str:
        joined = "\n".join(b["text"] for b in raw)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

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
        for b in raw:
            if b["category"] == "Table":
                segs = [b["text"]]
            else:
                segs = self._split_block(b["text"])
            for seg in segs:
                blocks.append(
                    {
                        "id": len(blocks),
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

    def _assemble_boundaries(self, blocks, client):
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
            for s, e in make_windows(blocks):
                texts = [blocks[k]["text"] for k in range(s, e)]
                try:
                    decisions.append((s, self._llm_boundaries(client, texts)))
                except (OllamaError, Exception) as exc:  # noqa: BLE001
                    log.warning("stage1_window_skipped", start=s, end=e, error=str(exc))
            llm_dec = reconcile(decisions)
        final = ["new"]
        for i in range(1, n):
            final.append(heur[i] if heur[i] != "uncertain" else llm_dec.get(i, "new"))
        return final

    @staticmethod
    def _merge_blocks(blocks, boundaries):
        parts, cur = [], None
        for i, b in enumerate(blocks):
            if boundaries[i] == "continuation" and cur is not None:
                cur["text"] += " " + b["text"]
                cur["source_ids"].append(b["id"])
            else:
                if cur is not None:
                    parts.append(cur)
                cur = {
                    "text": b["text"],
                    "source_ids": [b["id"]],
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

    def _semantic_merge_pass(self, parts, client):
        decisions = []
        for s, e in make_windows(parts):
            texts = [parts[k]["text"] for k in range(s, e)]
            try:
                decisions.append((s, self._llm_semantic_merge(client, texts)))
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                log.warning("stage15_window_skipped", start=s, end=e, error=str(exc))
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
            else:
                if cur is not None:
                    merged.append(cur)
                cur = {
                    "text": p["text"],
                    "source_ids": list(p.get("source_ids", [p["id"]])),
                    "category": p.get("category", ""),
                    "html": p.get("html"),
                }
        if cur is not None:
            merged.append(cur)
        for i, p in enumerate(merged):
            p["id"] = i
        return merged

    def semantic_merge(self, parts, client):
        if client is None or len(parts) < 2:
            return parts
        for npass in range(1, self.settings.semantic_merge_max_passes + 1):
            before = len(parts)
            parts = self._semantic_merge_pass(parts, client)
            log.info("stage15_pass", npass=npass, before=before, after=len(parts))
            if len(parts) == before:
                break
        return parts

    def to_logical_parts(
        self, raw: list[dict], client: OllamaClient | None
    ) -> list[dict]:
        blocks = self._split_into_segments(raw)
        log.info("stage1_split", blocks=len(raw), segments=len(blocks))
        parts = self._merge_blocks(blocks, self._assemble_boundaries(blocks, client))
        for i, p in enumerate(parts):
            p["id"] = i
        parts = self.semantic_merge(parts, client)
        log.info("stage1_done", parts=len(parts))
        return parts
