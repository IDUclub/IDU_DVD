"""Integration: the full ingestion + search pipeline on the live stack.

End-to-end through ``init_dependencies`` → IngestionService (real Ollama LLM markup + embeddings,
real Qdrant upsert, real Redis job/registry) → SearchService. This is the correctness check the
task asked for: docker-compose services + local Ollama. It is the slowest test (LLM passes).

Cleanup: the collection is dropped by ``temp_collection``; Redis keys are deleted explicitly.
"""

from __future__ import annotations

import pytest

from src.dvd_service.dto import SearchRequest

pytestmark = pytest.mark.integration


SMALL_DOC = [
    {
        "text": "СП 99.99999.2099 Тестовый свод правил. Общие положения.",
        "category": "Title",
        "html": None,
    },
    {"text": "1 Область применения", "category": "NarrativeText", "html": None},
    {
        "text": "Настоящий документ устанавливает требования к проверке систем.",
        "category": "NarrativeText",
        "html": None,
    },
    {"text": "2 Нормативные ссылки", "category": "NarrativeText", "html": None},
    {
        "text": "В документе использованы ссылки на стандарты по безопасности труда.",
        "category": "NarrativeText",
        "html": None,
    },
]


def test_ingest_then_search_end_to_end(
    temp_collection, require_redis, require_ollama, require_embedder, reset_dependencies
):
    from src.dependencies import init_dependencies

    deps = init_dependencies(temp_collection)
    content_hash = deps.parser.content_hash(SMALL_DOC)
    job_id = "itest-pipeline"

    try:
        result = deps.ingestion.ingest(
            "itest_doc.docx", SMALL_DOC, content_hash, job_id=job_id
        )

        # ingestion produced nodes, the job finished, and the document is registered
        assert result["nodes"] > 0
        assert deps.jobs.get(job_id)["status"] == "done"
        assert deps.registry.has_hash(content_hash)
        assert result["version"] in deps.registry.versions(result["name"])

        # the indexed content is searchable
        hits = deps.search.search(
            SearchRequest(query="требования к проверке систем", limit=5), None
        )
        assert hits.count >= 1
    finally:
        # Registry keys are scoped to the collection and cleared by temp_collection;
        # only the (unscoped) job status key needs explicit cleanup here.
        require_redis.r.delete(f"dvd:job:{job_id}")
