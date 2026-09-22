"""The VersionDetector (document head: name, version, administrative scope) — LLM (Ollama).

Fragment tagging used to live here too, but it now shares the single structure LLM pass
(see :mod:`dvd_service.modules.structure`), so this module only owns the head pass: the first
fragments of a document (title page, foreword, imprint) are the one place that states both
what the document is called and who issued it, so name/version detection and the
level/territory hints are answered by one structured-output call rather than two.

The hints are deliberately *hints*: the LLM names a territory in free text, and
:mod:`dvd_service.modules.territory` is what turns that into an Urban API territory id.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.api_clients import ChatClient, OllamaError

log = structlog.get_logger(__name__)

HEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "version": {"type": "string"},
        "level": {
            "type": "string",
            "enum": ["federal", "regional", "municipal", "unknown"],
        },
        "territory": {"type": "string"},
        "region": {"type": "string"},
        "federal_scope_evidence": {"type": "string"},
    },
    "required": [
        "name",
        "version",
        "level",
        "territory",
        "region",
        "federal_scope_evidence",
    ],
}
HEAD_SYSTEM = (
    "Тебе даны первые фрагменты документа (титул, предисловие, выходные данные). "
    "Верни шесть полей:\n"
    'name - КРАТКОЕ обозначение документа без редакции/изменений ("СП 19.13330.2019", '
    '"ГОСТ 12.1.004-91", или название, если обозначения нет).\n'
    "version - ПОЛНАЯ версия/редакция: обозначение + год + редакция/изменение, если указаны "
    '("СП 19.13330.2019 (с Изменением N 1)"). Если определить нельзя - верни "" в обоих.\n'
    "level - уровень действия документа: federal - общероссийский (СП, ГОСТ, СНиП, "
    "федеральный закон, приказ федерального министерства); regional - документ субъекта РФ "
    "(область, край, республика, город федерального значения); municipal - документ "
    "муниципального образования, района, города, поселения. Если определить нельзя - unknown.\n"
    "territory - название территории, на которую распространяется документ, ИМЕНИТЕЛЬНЫЙ "
    'падеж, без типа документа ("Ленинградская область", "Выборгский муниципальный район", '
    '"Санкт-Петербург"). Для federal верни "".\n'
    "region - название субъекта РФ, в котором находится эта территория (для level=municipal "
    'помогает различить одноимённые районы). Если неизвестно - верни "".\n'
    "federal_scope_evidence - независимо от выбранного level проверь, есть ли признаки "
    "общероссийского действия самого документа: явно указанная область действия на всю РФ "
    "или собственный статус федерального закона, СП, ГОСТ, СНиП, акта федерального органа. "
    "Верни дословную цитату из данных фрагментов, подтверждающую это. Упоминание России "
    "в адресе, ссылка на чужой федеральный акт или отсутствие местной территории не являются "
    'подтверждением. Если таких признаков нет или область действия явно местная - верни "".'
)


@dataclass(frozen=True)
class DocumentHead:
    """What the first fragments of a document say about it."""

    name: str
    version: str
    level_hint: str = "unknown"
    territory_hint: str = ""
    region_hint: str = ""
    federal_scope_evidence: str = ""


class VersionDetector:
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def detect_head(self, parts, client: ChatClient, head: int = 14) -> DocumentHead:
        """Name, version and administrative-scope hints from the document head — one LLM call.

        A failure is never fatal: the caller falls back to "unknown" identity and leaves the
        scope untagged, which the backfill job picks up later.
        """
        head_text = "\n".join(p["text"][:300] for p in parts[:head])
        try:
            data = client.chat(HEAD_SYSTEM, head_text, HEAD_SCHEMA)
            name = (data.get("name") or "").strip()
            version = (data.get("version") or "").strip()
            evidence = (data.get("federal_scope_evidence") or "").strip()
            # Only source-grounded evidence may enable the country fallback.
            if " ".join(evidence.split()) not in " ".join(head_text.split()):
                evidence = ""
            return DocumentHead(
                name=name or "unknown",
                version=version or name or "unknown",
                level_hint=(data.get("level") or "unknown").strip().lower(),
                territory_hint=(data.get("territory") or "").strip(),
                region_hint=(data.get("region") or "").strip(),
                federal_scope_evidence=evidence,
            )
        except (OllamaError, Exception) as exc:  # noqa: BLE001
            log.warning("version_detect_failed", error=str(exc))
            return DocumentHead(name="unknown", version="unknown")

    def detect(self, parts, client: ChatClient, head: int = 14) -> tuple[str, str]:
        """Name + version only — the identity half of :meth:`detect_head`."""
        detected = self.detect_head(parts, client, head)
        return detected.name, detected.version
