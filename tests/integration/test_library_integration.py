"""Integration: the document read API + source grounding on the live stack.

Ingests a small document end-to-end (real Ollama + Qdrant + Redis), then exercises
``LibraryService`` — the consumer-facing read API — and asserts that the general-purpose identity,
source grounding and external-id resolution survive a real round-trip.

Cleanup: the collection and scoped Redis registry keys are dropped by ``temp_collection``; only the
unscoped job key is deleted explicitly.
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


class _MockEmbedder:
    """TODO: replace with the real giga-vectorizer when CI/local integration has GPU access."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def __enter__(self) -> "_MockEmbedder":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(text, idx) for idx, text in enumerate(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text, 0)

    def _vec(self, text: str, salt: int) -> list[float]:
        vec = [0.0] * self.dim
        vec[(len(text) + salt) % self.dim] = 1.0
        return vec


def test_library_read_api_and_grounding_end_to_end(
    temp_collection, require_redis, require_ollama, reset_dependencies, monkeypatch
):
    from src.dependencies import init_dependencies
    from src.dvd_service.services import dvd_service as svc

    monkeypatch.setattr(
        svc, "create_embedder", lambda: _MockEmbedder(temp_collection.vector_size)
    )

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
