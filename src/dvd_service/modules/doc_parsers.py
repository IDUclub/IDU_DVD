"""Stage 1 + 1.5: the DocumentParser class — extraction, splitting, joining, semantic merge.

Also: content_hash (for deduplication) and preservation of table HTML.
"""

from __future__ import annotations

import hashlib
import os
import re

import structlog

from src.api_clients import ChatClient
from src.common.config import Settings
from src.dvd_service.modules.docx_reader import DocxReader
from src.dvd_service.modules.range_partitioning import RangePartitioner
from src.dvd_service.modules.reference_patterns import DESIGNATION_PREFIXES
from src.dvd_service.modules.source_structure import SourceStructure
from src.dvd_service.modules.windowing import (
    chat_window,
    make_windows,
    map_concurrent,
    reconcile,
)

log = structlog.get_logger(__name__)

PARSER_VERSION = (
    "dvd-parser-6"  # Keep designation codes wrapped onto a new line in their clause
)

SKIP_CATEGORIES = {"Header", "Footer", "PageBreak"}

# Clause numbers have up to three digits per level (as SourceStructure.NUMBER). A longer
# group is a designation code wrapped onto a new line: "СП\n59.13330 и СП 136.13330."
CLAUSE_NUMBER = r"\d{1,3}(?:\.\d{1,3})*"
LIST_MARKER = re.compile(
    rf"^\s*({CLAUSE_NUMBER}[.)]?|\w[.)]|[IVXLCDM]+[.)]|[-*•·–—‣◦])\s+\S", re.U
)
# Count the introduction, all item text/markers and the spaces used by _merge_blocks.
STRUCTURAL_GROUP_MAX_CHARS = 512
TERMINALS = (".", "!", "?", ";", ":", "…", "。", "！", "？", "»", '"', ")")
OPEN_START = ("[", "(", "«", '"')
# A new line can start a list item; an inline number may be a reference or date.
MARKER_INLINE = re.compile(
    rf"\n[ \t]*(?=(?:{CLAUSE_NUMBER}[.)]?|[а-яёa-z][.)]|[IVXLCDM]+[.)]|[-*•·–—‣◦])\s)",
    re.I | re.U,
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
    rf"^\s*(\d{{1,3}}(?:\.\d{{1,3}})+[.)]?|\d{{1,3}}[.)]|[IVXLCDM]+[.)]|[а-яёa-z][.)])\s+\S",
    re.I | re.U,
)
# A line ending in a document prefix is continued by that document's code on the next
# line, even when the code looks like a clause number: "ГОСТ\n12.4.026 Знаки".
DESIGNATION_TAIL = re.compile(
    r"(?:^|[\s(«\"])(?:"
    + "|".join(p.replace(" ", r"\s+") for p in (*DESIGNATION_PREFIXES, "ГН"))
    + r"|№|N|п\.|пп\.)\s*$",
    re.U,
)


def starts_new_marker(text: str) -> bool:
    return bool(LIST_MARKER.match(text.strip()))


def is_numbered_head(text: str) -> bool:
    return bool(NUMBERED_HEAD.match(text.strip()))


def continues_designation(prev: str, cur: str) -> bool:
    """``cur`` carries on the document designation that ``prev`` ends with."""
    return bool(DESIGNATION_TAIL.search(prev)) and cur.lstrip()[:1].isdigit()


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
        self.range_partitioner = RangePartitioner(settings, STRUCTURAL_GROUP_MAX_CHARS)

    @property
    def version(self) -> str:
        return PARSER_VERSION + (
            "-semantic1-ranges"
            if self.settings.logical_partition_mode == "ranges"
            else ""
        )

    def __repr__(self) -> str:
        s = self.settings
        return (
            f"{type(self).__name__}(split_sentences={s.split_sentences}, "
            f"sent_min_len={s.sent_min_len}, window_chars={s.window_chars}, "
            f"semantic_merge_max_passes={s.semantic_merge_max_passes})"
        )

    # --- extraction and hashing (for dedup before the heavy LLM pass) ---
    def extract_raw(self, path: str) -> list[dict]:
        if os.path.splitext(str(path))[1].lower() == ".docx":
            return DocxReader().read(path)
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
        if SourceStructure.starts_part(cur) or SourceStructure.NOTE.match(prev):
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

    @staticmethod
    def _line_segments(text: str) -> list[str]:
        """Split at lines that start a marker, not at a wrapped designation code."""
        segments, start = [], 0
        for m in MARKER_INLINE.finditer(text):
            if continues_designation(text[start : m.start()], text[m.end() :]):
                continue
            segments.append(text[start : m.start()])
            start = m.end()
        segments.append(text[start:])
        return [s.strip() for s in segments if s.strip()]

    def _split_block(self, text: str) -> list[str]:
        segments = self._line_segments(text)
        if not self.settings.split_sentences or SourceStructure.starts_part(text):
            return segments
        out: list[str] = []
        for seg in segments:
            out.extend(
                self._split_sentences(seg)
                if len(seg) > self.settings.sent_min_len and not starts_new_marker(seg)
                else [seg]
            )
        return out

    def _split_into_segments(self, raw):
        blocks = []
        for ri, b in enumerate(raw):
            if b["category"] == "Table" or b.get("source_paragraph"):
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
    @staticmethod
    def _structural_ranges(blocks) -> dict[int, int]:
        """Contiguous source subtrees: numbered provisions and introduced lists.

        Decimal prefixes define subclauses (1 -> 1.1 -> 1.1.1). Inside an explicit
        article, decimal parts are siblings, matching HierarchyBuilder; 1) and а)
        remain nested items. Headings, notes, tables and unmarked prose end a run.
        """
        stack = []
        ends = {}
        in_article = False
        for i, block in enumerate(blocks):
            text = block["text"]
            anchor = SourceStructure.anchor(text)
            typ = anchor.get("type")
            if typ in {"article", "chapter", "section"}:
                in_article = typ == "article"
            marker = LIST_MARKER.match(text.strip())
            eligible = block["category"] != "Table" and not typ
            number = anchor.get("numbering", "").rstrip(")")
            decimal = bool(number) and all(p.isdigit() for p in number.split("."))
            if decimal:
                kind = (
                    "numeric_item"
                    if anchor.get("source_delimiter") == ")"
                    else "decimal"
                )
            elif marker and re.fullmatch(r"[^\W\d_][.)]", marker[1]):
                kind = "letter_item"
            else:
                kind = "marker" if marker else "intro"
            introduced = eligible and text.rstrip().endswith(":")
            candidate = eligible and (marker is not None or introduced)
            parent = None
            if candidate and marker:
                for node in reversed(stack):
                    if (
                        (
                            kind == "decimal"
                            and not in_article
                            and node["kind"] == "decimal"
                            and number.startswith(node["number"] + ".")
                        )
                        or (kind == "numeric_item" and node["kind"] == "decimal")
                        or (
                            kind == "letter_item"
                            and node["kind"] in {"decimal", "numeric_item"}
                        )
                        or (
                            node["introduced"]
                            and (node["kind"] == "intro" or kind == "marker")
                        )
                    ):
                        parent = node["index"]
                        break
            while stack and stack[-1]["index"] != parent:
                node = stack.pop()
                ends[node["index"]] = i
            if candidate:
                stack.append(
                    {
                        "index": i,
                        "kind": kind,
                        "number": number,
                        "introduced": introduced,
                    }
                )
        for node in stack:
            ends[node["index"]] = len(blocks)
        return {start: end for start, end in ends.items() if end > start + 1}

    @classmethod
    def _structural_boundaries(cls, blocks) -> dict[int, str]:
        """Choose the largest fitting subtree, descending only when it exceeds 512.

        Mark every chosen fragment so the semantic merge cannot undo this decision.
        The size includes source markers and the spaces inserted by _merge_blocks.
        """
        lengths = [0]
        for block in blocks:
            block.pop("_group_atomic", None)
            lengths.append(lengths[-1] + len(block["text"]) + 1)
        ranges = cls._structural_ranges(blocks)
        boundaries = {}
        start = protected_end = 0
        while start < len(blocks):
            end = ranges.get(start, start + 1)
            if end > start + 1:
                protected_end = max(protected_end, end)
            if start >= protected_end:
                start += 1
                continue
            size = lengths[end] - lengths[start] - 1
            if "char_start" in blocks[start]:
                size = blocks[end - 1]["char_end"] - blocks[start]["char_start"]
            stop = end if size <= STRUCTURAL_GROUP_MAX_CHARS else start + 1
            for i in range(start, stop):
                blocks[i]["_group_atomic"] = True
                boundaries[i] = "new" if i == start else "continuation"
            boundaries[stop] = "new"
            start = stop
        boundaries.pop(len(blocks), None)
        return boundaries

    def source_units(self, raw):
        """Candidate boundaries with exact offsets in the extracted source text."""
        source_text, spans = self.source_index(raw)
        units = []
        for src, block in enumerate(raw):
            text = block["text"]
            pieces = (
                [text]
                if block["category"] == "Table"
                or starts_new_marker(text)
                or SourceStructure.starts_part(text)
                else self._split_block(text)
            )
            cursor = 0
            for piece in pieces:
                if not piece:
                    continue
                pos = text.find(piece, cursor)
                if pos < 0:
                    raise ValueError("Исходный сегмент не найден без изменения текста")
                units.append(
                    {
                        "id": len(units),
                        "src_ids": [src],
                        "category": block["category"],
                        "html": block.get("html"),
                        "char_start": spans[src]["start"] + pos,
                    }
                )
                cursor = pos + len(piece)
        if units:
            units[0]["char_start"] = 0
        # A code wrapped into the next paragraph stays in the unit that names its document.
        joined = units[:1]
        for unit in units[1:]:
            prev = joined[-1]
            if prev["category"] != "Table" and unit["category"] != "Table":
                prev_text = source_text[prev["char_start"] : unit["char_start"]]
                if continues_designation(prev_text, source_text[unit["char_start"] :]):
                    prev["src_ids"] = sorted({*prev["src_ids"], *unit["src_ids"]})
                    continue
            joined.append(unit)
        units = joined
        for i, unit in enumerate(units):
            unit["id"] = i
        for i, unit in enumerate(units):
            unit["char_end"] = (
                units[i + 1]["char_start"] if i + 1 < len(units) else len(source_text)
            )
            unit["text"] = source_text[unit["char_start"] : unit["char_end"]]
        return source_text, units

    def prepare_range_units(self, source_text, units):
        """Preserve numbered leaves until their types and parentage are known."""
        prepared = []
        previous_locked = False
        for unit in units:
            anchor = SourceStructure.anchor(unit["text"])
            locked = unit["category"] == "Table" or bool(anchor)
            prepared.append(
                {
                    **unit,
                    "must_start": not prepared
                    or locked
                    or previous_locked
                    or starts_new_marker(unit["text"]),
                }
            )
            previous_locked = locked
        return prepared

    def _llm_boundaries(self, client: ChatClient, window_texts):
        rows = chat_window(
            client, BOUNDARY_SYSTEM, window_texts, BOUNDARY_SCHEMA, "blocks"
        )
        return {item["id"]: item["boundary"] for item in rows}

    def _assemble_boundaries(self, blocks, client, on_progress=None):
        n = len(blocks)
        if not n:
            return []
        structural_boundaries = self._structural_boundaries(blocks)
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

            def process(window):
                s, e = window
                texts = [blocks[k]["text"] for k in range(s, e)]
                try:
                    return s, self._llm_boundaries(client, texts)
                except Exception as exc:  # noqa: BLE001
                    log.warning("stage1_window_failed", start=s, end=e, error=str(exc))
                    raise

            results = map_concurrent(
                process, windows, max_workers=self.settings.llm_concurrency
            )
            for done, decision in enumerate(results, 1):
                decisions.append(decision)
                if on_progress:
                    on_progress(done, len(windows), "boundaries")
            llm_dec = reconcile(decisions)
        final = ["new"]
        for i in range(1, n):
            if continues_designation(blocks[i - 1]["text"], blocks[i]["text"]):
                # Source structure is misread here: the "number" is a document code.
                final.append("continuation")
                continue
            final.append(
                structural_boundaries.get(
                    i, heur[i] if heur[i] != "uncertain" else llm_dec.get(i, "new")
                )
            )
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
                if b.get("_group_atomic"):
                    cur["_group_atomic"] = True
        if cur is not None:
            parts.append(cur)
        return parts

    # --- semantic merge (Stage 1.5) ---
    def _llm_semantic_merge(self, client, window_texts):
        rows = chat_window(
            client, SEMANTIC_MERGE_SYSTEM, window_texts, SEMANTIC_MERGE_SCHEMA, "parts"
        )
        return {
            item["id"]: ("continuation" if item["merge_with_previous"] else "new")
            for item in rows
        }

    def _semantic_merge_pass(self, parts, client, on_progress=None, npass=1):
        decisions = []
        windows = list(make_windows(parts))

        def process(window):
            s, e = window
            texts = [parts[k]["text"] for k in range(s, e)]
            try:
                return s, self._llm_semantic_merge(client, texts)
            except Exception as exc:  # noqa: BLE001
                log.warning("stage15_window_failed", start=s, end=e, error=str(exc))
                raise

        results = map_concurrent(
            process, windows, max_workers=self.settings.llm_concurrency
        )
        for done, decision in enumerate(results, 1):
            decisions.append(decision)
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
                and not p.get("_group_atomic")
                and not cur.get("_group_atomic")
                and not is_numbered_head(p["text"])
                and not SourceStructure.starts_part(p["text"])
                and not SourceStructure.NOTE.match(cur["text"])
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
                if p.get("_group_atomic"):
                    cur["_group_atomic"] = True
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
        self, raw: list[dict], client: ChatClient | None, on_progress=None
    ) -> list[dict]:
        if self.settings.logical_partition_mode == "ranges":
            source_text, units = self.source_units(raw)
            units = self.prepare_range_units(source_text, units)
            ranges = (
                self.range_partitioner.partition(units, client, on_progress)
                if client is not None
                else [(i, i) for i in range(len(units))]
            )
            return self.range_partitioner.materialize(source_text, units, ranges)
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
