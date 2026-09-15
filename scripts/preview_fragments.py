#!/usr/bin/env python3
"""Preview final fragment content without embeddings or database connections.

Run from the repository root: uv run python scripts/preview_fragments.py document.docx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if TYPE_CHECKING:
    from src.api_clients import ChatClient
    from src.common.config import Settings


class FragmentPreview:
    """Run the content stages of IngestionService.ingest with the same settings.

    Keep the stage order aligned with ingest; the parity test compares this output
    with actual payloads captured at its Qdrant boundary.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def progress(done: int, total: int, detail: str | None = None) -> None:
        print(f"  {detail or ''} {done}/{total}", file=sys.stderr, flush=True)

    def build(self, source: Path, client: ChatClient) -> dict:
        from src.dvd_service.modules.doc_parsers import DocumentParser
        from src.dvd_service.modules.fragment_structure import annotate_fragments
        from src.dvd_service.modules.hierarchy import HierarchyBuilder
        from src.dvd_service.modules.structure import StructureTagger
        from src.dvd_service.services.dvd_service import IngestionService

        parser = DocumentParser(self.settings)
        structure = StructureTagger(self.settings)
        hierarchy = HierarchyBuilder()
        print("Извлечение исходного текста…", file=sys.stderr, flush=True)
        raw = parser.extract_raw(str(source))
        if not raw:
            raise ValueError("Документ не содержит поддерживаемого текста или таблиц")
        print(
            "Логические части и семантическое объединение…", file=sys.stderr, flush=True
        )
        parts = parser.to_logical_parts(raw, client, on_progress=self.progress)
        print("Структурная разметка и теги…", file=sys.stderr, flush=True)
        structure.tag(parts, client, on_progress=self.progress)
        ranks = structure.numbering_ranks(parts)
        semantic = self.settings.logical_partition_mode == "ranges"
        assembly_parts = hierarchy.coalesce_title_pages(parts) if semantic else parts
        tree = hierarchy.build(
            assembly_parts, ranks, title=source.stem, semantic=semantic
        )
        if not semantic:
            hierarchy.cap_unnumbered_nesting(tree)
        hierarchy.group_amendment(tree)
        if semantic:
            hierarchy.assemble_semantic(tree)
        nodes = annotate_fragments(hierarchy.flatten(tree))
        _, spans = parser.source_index(raw)
        doc_id = str(uuid.uuid4())
        for order, node in enumerate(nodes):
            node.update(IngestionService._grounding(node, spans, doc_id))
            node["order"] = order
            node["src_block_ids"] = node.pop("src_ids", [])
            # Preserve the payload value and separately show the actual embedder input.
            node["embedding_text"] = node.get("search_text", node["text"])
            node.setdefault("search_text", None)
        return {
            "source": str(source.resolve()),
            "parser_version": parser.version,
            "logical_partition_mode": self.settings.logical_partition_mode,
            "llm_provider": self.settings.llm_provider,
            "llm_model": (
                self.settings.llm_model
                if self.settings.llm_provider == "openai"
                else self.settings.ollama_model
            ),
            "content_hash": parser.content_hash(raw),
            "raw_blocks": len(raw),
            "fragment_count": len(nodes),
            "note": (
                "Предпросмотр содержимого фрагментов, не полный Qdrant payload. "
                "Векторы, версия/идентичность документа, территория и междокументные "
                "ссылки не вычисляются. UUID временные. Повторный запуск LLM может "
                "дать другую нарезку. Записей в БД нет."
            ),
            "fragments": nodes,
        }

    @staticmethod
    def render(report: dict) -> str:
        lines = [
            f"Источник: {report['source']}",
            f"Парсер: {report['parser_version']}",
            f"Фрагментов: {report['fragment_count']}",
            report["note"],
        ]
        for node in report["fragments"]:
            lines.extend(
                [
                    "",
                    "=" * 88,
                    f"#{node['order']} | {node['type']} | {node['kind']} | "
                    f"номер: {node['numbering'] or '—'} | глубина: {node['depth']}",
                    f"ID: {node['id']} | родитель: {node['parent_id'] or '—'}",
                    f"Путь: {node['breadcrumb']}",
                    f"Контейнер: {node['is_container']} | "
                    f"исходный диапазон: [{node['char_start']}, {node['char_end']})",
                    f"Теги: {', '.join(node['tags'])}",
                    "",
                    "ТЕКСТ ФРАГМЕНТА:",
                    node["text"],
                ]
            )
            if node["embedding_text"] != node["text"]:
                lines.extend(["", "ТЕКСТ ДЛЯ ВЕКТОРИЗАЦИИ:", node["embedding_text"]])
            if node.get("source_text") is not None:
                lines.extend(["", "ТОЧНЫЙ ИСХОДНЫЙ ТЕКСТ:", node["source_text"]])
            if node.get("table_html"):
                lines.extend(["", "ТАБЛИЦА (HTML):", node["table_html"]])
        return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(
        description="Просмотр финальной нарезки документа без записи в БД. Требуется LLM из .env."
    )
    cli.add_argument(
        "source", type=Path, help="Исходный .docx, .txt, .md, .html или .htm"
    )
    cli.add_argument(
        "--output-dir",
        type=Path,
        help="Папка отчётов (по умолчанию _uploads/preview/<имя>)",
    )
    args = cli.parse_args(argv)
    source = args.source.resolve()
    if not source.is_file():
        cli.error(f"Файл не найден: {source}")
    if source.suffix.lower() not in {".docx", ".txt", ".md", ".html", ".htm"}:
        cli.error(f"Неподдерживаемый формат: {source.suffix}")
    output = args.output_dir or Path("_uploads/preview") / source.stem
    paths = [output / "fragments.json", output / "fragments.txt"]
    if any(path.resolve() == source for path in paths):
        cli.error("Путь отчёта совпадает с исходным файлом")

    # Delay application imports so --help works without service configuration.
    # Settings requires auth fields even though this process never uses service auth.
    for key, value in {
        "DVD_SERVICE_AUTH_SERVER_URL": "http://127.0.0.1",
        "DVD_SERVICE_AUTH_REALM": "unused-preview",
        "DVD_SERVICE_AUTH_CLIENT_ID": "unused-preview",
        "DVD_SERVICE_AUTH_CLIENT_SECRET": "unused-preview",
    }.items():
        os.environ.setdefault(key, value)

    from src.api_clients import create_llm
    from src.common.config import settings

    client = create_llm()
    try:
        report = FragmentPreview(settings).build(source, client)
    finally:
        client.close()
    output.mkdir(parents=True, exist_ok=True)
    paths[0].write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths[1].write_text(FragmentPreview.render(report), encoding="utf-8")
    counts = Counter(node["kind"] for node in report["fragments"])
    print(f"Готово: {report['fragment_count']} фрагментов ({dict(counts)}).")
    for path in paths:
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
