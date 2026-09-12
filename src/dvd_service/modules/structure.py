"""Stage 2/3/3.5: the StructureTagger class — type/numbering/relation/block + tags, category, rank.

The single structure pass also emits per-fragment search tags (previously a separate LLM pass):
one windowed LLM call now returns both the structural fields and the tags for every part, halving
the LLM traffic over the document. Tags then ride along the hierarchy onto the flat nodes.
"""

from __future__ import annotations

import re

import structlog

from src.api_clients import ChatClient
from src.common.config import Settings
from src.dvd_service.modules.source_structure import SourceStructure
from src.dvd_service.modules.windowing import (
    chat_window,
    make_windows,
    map_concurrent,
    reconcile,
)

log = structlog.get_logger(__name__)

STRUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "type": {"type": "string"},
                    "numbering": {"type": "string"},
                    "fragment_name": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": ["top", "deeper", "same", "shallower"],
                    },
                    "block": {"type": "string", "enum": ["main", "amendment"]},
                    "tags": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "id",
                    "type",
                    "numbering",
                    "fragment_name",
                    "relation",
                    "block",
                    "tags",
                ],
            },
        }
    },
    "required": ["nodes"],
}
STRUCT_SYSTEM = (
    "fragment_name — собственное наименование фрагмента: заголовок, определяемый термин "
    "или подпись таблицы. Копируй дословный непрерывный фрагмент исходного текста без номера; "
    "если явного наименования нет — пустая строка. Не придумывай краткое описание.\n"
    "Ты анализируешь структуру документа ЛЮБОГО типа и языка. Дан список логических частей по "
    "порядку, каждая с id. Для каждой части верни все поля схемы.\n"
    "type - вид структурного элемента ПО СОДЕРЖАНИЮ (title_page, toc, preface, introduction, "
    "chapter, section, clause, subclause, list_item, paragraph, table, note, definition, "
    "appendix, conclusion, bibliography, reference). Иначе придумай краткий snake_case. Не other.\n"
    'numbering - СОБСТВЕННЫЙ номер части дословно из начала текста ("1", "4.2", "а)"). '
    'Если своего номера нет - "". НЕ принимай за номер коды/обозначения ДРУГИХ документов '
    "(ГОСТ 9238, СП 108.13330.2012), номера и даты законов, номера таблиц/рисунков.\n"
    "relation - глубина ОТНОСИТЕЛЬНО ПРЕДЫДУЩЕЙ части: top/deeper/same/shallower.\n"
    'block - "amendment", если часть относится к изменению/поправке; иначе "main".\n'
    "tags - от 2 до 6 ТЕГОВ (ключевые темы, объекты, термины для поиска): короткие (1-3 слова), "
    "в нижнем регистре, на языке части, без знаков препинания. Для служебных/малосодержательных "
    "частей - пустой список.\n"
    "Опирайся на смысл и нумерацию. Текст не меняй. Верни все поля по каждому id."
)

SYNONYMS = {
    "cover": "title_page",
    "title": "title_page",
    "titlepage": "title_page",
    "contents": "toc",
    "table_of_contents": "toc",
    "содержание": "toc",
    "foreword": "preface",
    "предисловие": "preface",
    "intro": "introduction",
    "введение": "introduction",
    "part": "chapter",
    "раздел": "chapter",
    "глава": "chapter",
    "subsection": "section",
    "подраздел": "section",
    "point": "clause",
    "пункт": "clause",
    "подпункт": "subclause",
    "item": "list_item",
    "enumeration": "list_item",
    "заключение": "conclusion",
    "приложение": "appendix",
    "references": "bibliography",
    "литература": "bibliography",
}


class StructureTagger:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(window_max_items={self.settings.window_max_items})"
        )

    @staticmethod
    def categorize(raw_type: str) -> str:
        t = (raw_type or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not t:
            return "paragraph"
        return SYNONYMS.get(t, t)

    @staticmethod
    def strip_leading_numbering(text: str, numbering: str) -> str:
        if not numbering:
            return text
        m = re.match(r"\s*" + re.escape(numbering) + r"(?!\w|\.\d)[.)\s]*", text)
        if m:
            rest = text[m.end() :]
            return rest if rest.strip() else text
        return text

    @staticmethod
    def numbering_rank(num: str):
        s = (num or "").strip().rstrip(".)").strip()
        if not s:
            return None
        parts = s.split(".")
        if not all(p.isdigit() for p in parts):
            return None
        if not (1 <= len(parts) <= 6):
            return None
        if any(len(p) >= 4 for p in parts):
            return None
        return len(parts)

    def numbering_ranks(self, parts) -> dict[str, int]:
        labels = sorted({p.get("numbering", "") for p in parts if p.get("numbering")})
        ranks = {l: self.numbering_rank(l) for l in labels}
        return {l: r for l, r in ranks.items() if r}

    @staticmethod
    def _clean_tags(raw) -> list[str]:
        return [str(x).strip().lower() for x in (raw or []) if str(x).strip()]

    def _llm_structure(self, client: ChatClient, window_texts):
        rows = chat_window(client, STRUCT_SYSTEM, window_texts, STRUCT_SCHEMA, "nodes")
        return {
            it["id"]: (
                it["type"],
                it.get("numbering", ""),
                it["relation"],
                it.get("block", "main"),
                self._clean_tags(it.get("tags")),
                it.get("fragment_name", ""),
            )
            for it in rows
        }

    def tag(self, parts, client: ChatClient, on_progress=None) -> list[dict]:
        decisions = []
        windows = list(make_windows(parts, max_items=self.settings.window_max_items))

        def process(window):
            s, e = window
            texts = [parts[k]["text"] for k in range(s, e)]
            try:
                return s, self._llm_structure(client, texts)
            except Exception as exc:  # noqa: BLE001
                log.warning("stage2_window_failed", start=s, end=e, error=str(exc))
                raise

        results = map_concurrent(
            process, windows, max_workers=self.settings.llm_concurrency
        )
        for done, decision in enumerate(results, 1):
            decisions.append(decision)
            if on_progress:
                on_progress(done, len(windows))
        tags = reconcile(decisions)
        in_article = False
        for p in parts:
            t = tags.get(p["id"])
            if t is None:
                (
                    p["raw_type"],
                    p["numbering"],
                    p["relation"],
                    p["block"],
                    p["tags"],
                    p["fragment_name"],
                ) = ("paragraph", "", "deeper", "main", [], "")
            else:
                (
                    p["raw_type"],
                    p["numbering"],
                    p["relation"],
                    p["block"],
                    p["tags"],
                    p["fragment_name"],
                ) = t
            anchor = SourceStructure.anchor(p["text"])
            if anchor.get("type") in {"article", "chapter", "section"}:
                in_article = anchor["type"] == "article"
            if in_article and anchor.get("numbering") and not anchor.get("type"):
                anchor["type"] = (
                    "list_item" if anchor.get("source_delimiter") == ")" else "clause"
                )
            if anchor:
                p.update(anchor)
                if anchor.get("type"):
                    p["raw_type"] = anchor["type"]
            elif p["numbering"] and not re.match(
                r"^\s*" + re.escape(p["numbering"]) + r"(?:[.)]?\s+)", p["text"]
            ):
                p["numbering"] = ""
            if anchor.get("type") not in {"article", "chapter", "section"}:
                p["text"] = self.strip_leading_numbering(p["text"], p["numbering"])
            p["type"] = self.categorize(
                p["raw_type"]
            )  # NB: do not touch p['category'] (from unstructured)
        log.info("stage2_done", parts=len(parts))
        return parts
