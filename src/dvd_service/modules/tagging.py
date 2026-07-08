"""The VersionDetector (document name + version) class — LLM (Ollama).

Fragment tagging used to live here too, but it now shares the single structure LLM pass
(see :mod:`dvd_service.modules.structure`), so this module only owns version detection.
"""

from __future__ import annotations

import structlog

from src.api_clients import OllamaClient, OllamaError

log = structlog.get_logger(__name__)

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
