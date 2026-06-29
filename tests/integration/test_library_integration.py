"""Integration: the document read API + source grounding on the live stack.

Ingests a small document end-to-end (real Ollama + Qdrant + Redis), then exercises
``LibraryService`` — the consumer-facing read API — and asserts that the general-purpose identity,
source grounding and external-id resolution survive a real round-trip.

Cleanup: the collection is dropped by ``temp_collection``; Redis keys are deleted explicitly.
"""

from __future__ import annotations

import pytest

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
]


def test_library_read_api_and_grounding_end_to_end(
    temp_collection, require_redis, require_ollama, reset_dependencies
):
    from src.dependencies import init_dependencies

    deps = init_dependencies(temp_collection)
    content_hash = deps.parser.content_hash(SMALL_DOC)
    job_id = "itest-library"
    name = None
    doc_id = None

    try:
        result = deps.ingestion.ingest(
            "itest_lib.docx",
            SMALL_DOC,
            content_hash,
            job_id=job_id,
            doc_type="regulation",
            corpus="norms",
            lang="ru",
            external_ids={"code": "СП 99.99999.2099"},
        )
        name = result["name"]
        doc_id = result["doc_id"]

        # listing sees the document with its general-purpose identity
        listing = deps.library.list_documents()
        assert any(d.doc_id == doc_id for d in listing.documents)

        # full read: assembled text + ordered fragments with source grounding
        detail = deps.library.get_document(doc_id)
        assert detail is not None
        assert detail.doc_type == "regulation" and detail.corpus == "norms"
        assert detail.version_id and detail.text
        assert detail.fragments
        assert [f.order for f in detail.fragments] == sorted(
            f.order for f in detail.fragments
        )
        grounded = [f for f in detail.fragments if f.char_start is not None]
        assert grounded, "fragments must carry source offsets after a real ingest"
        g = grounded[0]
        assert g.char_end > g.char_start and g.span_id

        # resolution by external id / lookup key
        found = deps.library.find_documents("СП 99.99999.2099")
        assert any(d.doc_id == doc_id for d in found.documents)
    finally:
        require_redis.r.delete(f"dvd:job:{job_id}")
        require_redis.r.delete(f"dvd:hash:{content_hash}")
        if doc_id:
            require_redis.r.delete(f"dvd:doc:{doc_id}")
            require_redis.r.srem("dvd:docs", doc_id)
        if name:
            require_redis.r.delete(f"dvd:versions:{name}")
            require_redis.r.srem("dvd:names", name)
