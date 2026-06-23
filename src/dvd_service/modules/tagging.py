"""The Tagger (node tags) and VersionDetector (document name + version) classes — LLM (Ollama)."""

from __future__ import annotations

import structlog

from src.api_clients import OllamaClient, OllamaError
from src.common.config import Settings
from src.dvd_service.modules.windowing import make_windows

log = structlog.get_logger(__name__)

TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "tags"],
            },
        }
    },
    "required": ["nodes"],
}
TAG_SYSTEM = (
    "Дан пронумерованный список фрагментов документа, каждый с id. Для каждого фрагмента выдели "
    "от 2 до 6 ТЕГОВ — ключевые темы, объекты и термины, по которым фрагмент стоит искать. "
    "Теги короткие (1-3 слова), в нижнем регистре, на языке фрагмента, без знаков препинания. "
    "Если фрагмент служебный/малосодержательный — пустой список. Верни теги по каждому id."
)

VERSION_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "version": {"type": "string"}},
    "required": ["name", "version"],
}
VERSION_SYSTEM = (
    "Тебе даны первые фрагменты документа (титул, предисловие, выходные данные). Верни два поля:\n"
    'name - КРАТКОЕ обозначение документа без редакции/изменений ("СП 19.13330.2019", '
    '"ГОСТ 12.1.004-91", или название, если обозначения нет).\n'
    "version - ПОЛНАЯ версия/редакция: обозначение + год + редакция/изменение, если указаны "
    '("СП 19.13330.2019 (с Изменением N 1)"). Если определить нельзя - верни "" в обоих.'
)


class Tagger:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(window_max_items={self.settings.window_max_items})"
        )

    def _llm_tags(self, client: OllamaClient, window_texts):
        user = "\n".join("[%d] %s" % (i, t[:500]) for i, t in enumerate(window_texts))
        data = client.chat(TAG_SYSTEM, user, TAG_SCHEMA)
        return {
            it["id"]: [
                str(x).strip().lower() for x in it.get("tags", []) if str(x).strip()
            ]
            for it in data["nodes"]
        }

    def tag_nodes(self, nodes, client: OllamaClient) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for s, e in make_windows(
            nodes, overlap=0, max_items=self.settings.window_max_items
        ):
            window = nodes[s:e]
            try:
                local = self._llm_tags(client, [n["text"] for n in window])
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                log.warning("tagging_window_skipped", start=s, end=e, error=str(exc))
                continue
            for pos, n in enumerate(window):
                result[n["id"]] = local.get(pos, [])
        log.info("tagging_done", tagged=len(result))
        return result


class VersionDetector:
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def detect(self, parts, client: OllamaClient, head: int = 14) -> tuple[str, str]:
        head_text = "\n".join(p["text"][:300] for p in parts[:head])
        try:
            data = client.chat(VERSION_SYSTEM, head_text, VERSION_SCHEMA)
            name = (data.get("name") or "").strip()
            version = (data.get("version") or "").strip()
            return name or "unknown", version or name or "unknown"
        except (OllamaError, Exception) as exc:  # noqa: BLE001
            log.warning("version_detect_failed", error=str(exc))
            return "unknown", "unknown"
