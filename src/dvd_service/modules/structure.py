"""Stage 2/3/3.5: the StructureTagger class — type/numbering/relation/block, category, number rank."""

from __future__ import annotations

import re

import structlog

from src.api_clients import OllamaClient, OllamaError
from src.common.config import Settings
from src.dvd_service.modules.windowing import make_windows, reconcile

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
                    "relation": {
                        "type": "string",
                        "enum": ["top", "deeper", "same", "shallower"],
                    },
                    "block": {"type": "string", "enum": ["main", "amendment"]},
                },
                "required": ["id", "type", "numbering", "relation", "block"],
            },
        }
    },
    "required": ["nodes"],
}
STRUCT_SYSTEM = (
    "Ты анализируешь структуру документа ЛЮБОГО типа и языка. Дан список логических частей по "
    "порядку, каждая с id. Для каждой части верни четыре поля.\n"
    "type - вид структурного элемента ПО СОДЕРЖАНИЮ (title_page, toc, preface, introduction, "
    "chapter, section, clause, subclause, list_item, paragraph, table, note, definition, "
    "appendix, conclusion, bibliography, reference). Иначе придумай краткий snake_case. Не other.\n"
    'numbering - СОБСТВЕННЫЙ номер части дословно из начала текста ("1", "4.2", "а)"). '
    'Если своего номера нет - "". НЕ принимай за номер коды/обозначения ДРУГИХ документов '
    "(ГОСТ 9238, СП 108.13330.2012), номера и даты законов, номера таблиц/рисунков.\n"
    "relation - глубина ОТНОСИТЕЛЬНО ПРЕДЫДУЩЕЙ части: top/deeper/same/shallower.\n"
    'block - "amendment", если часть относится к изменению/поправке; иначе "main".\n'
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
        m = re.match(r"\s*" + re.escape(numbering) + r"(?![\w.])[.)\s]*", text)
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

    def _llm_structure(self, client: OllamaClient, window_texts):
        user = "\n".join("[%d] %s" % (i, t) for i, t in enumerate(window_texts))
        data = client.chat(STRUCT_SYSTEM, user, STRUCT_SCHEMA)
        return {
            it["id"]: (
                it["type"],
                it.get("numbering", ""),
                it["relation"],
                it.get("block", "main"),
            )
            for it in data["nodes"]
        }

    def tag(self, parts, client: OllamaClient) -> list[dict]:
        decisions = []
        for s, e in make_windows(parts, max_items=self.settings.window_max_items):
            texts = [parts[k]["text"] for k in range(s, e)]
            try:
                decisions.append((s, self._llm_structure(client, texts)))
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                log.warning("stage2_window_skipped", start=s, end=e, error=str(exc))
        tags = reconcile(decisions)
        for p in parts:
            t = tags.get(p["id"])
            if t is None:
                p["raw_type"], p["numbering"], p["relation"], p["block"] = (
                    "paragraph",
                    "",
                    "deeper",
                    "main",
                )
            else:
                p["raw_type"], p["numbering"], p["relation"], p["block"] = t
            p["text"] = self.strip_leading_numbering(p["text"], p["numbering"])
            p["type"] = self.categorize(
                p["raw_type"]
            )  # NB: do not touch p['category'] (from unstructured)
        log.info("stage2_done", parts=len(parts))
        return parts
