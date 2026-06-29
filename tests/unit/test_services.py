"""Unit tests for src/dvd_service/services/dvd_service — IngestionService and SearchService.

Wires the *real* pipeline modules (parser/structure/hierarchy/tagger/version) but fakes the
external boundaries: LLM (FakeOllama), Qdrant (FakeQdrantRepo) and Redis (fakeredis). This
exercises the full ingest+search orchestration without any live service.

Covers: end-to-end ingest (job status, registry, upsert), version override, version collision
resolution, error handling, search filtering, and context expansion.
"""

from __future__ import annotations

import pytest

import src.dvd_service.services.dvd_service as svc
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
from src.dvd_service.dto import SearchRequest
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import Tagger, VersionDetector
from src.dvd_service.services.dvd_service import (
    DocumentsService,
    IngestionService,
    LibraryService,
    SearchService,
)


@pytest.fixture
def wired(settings, fake_ollama, fake_qdrant, fake_redis, monkeypatch):
    """Build IngestionService + SearchService with real modules and faked boundaries."""
    monkeypatch.setattr(svc, "OllamaClient", lambda *a, **k: fake_ollama)
    redis_client = RedisClient(settings)
    jobs = JobStore(redis_client)
    registry = DocumentRegistry(redis_client)
    ingestion = IngestionService(
        DocumentParser(settings),
        StructureTagger(settings),
        HierarchyBuilder(),
        Tagger(settings),
        VersionDetector(),
        ReferenceExtractor(settings),
        ReferenceResolver(fake_qdrant, registry, settings),
        fake_qdrant,
        registry,
        jobs,
        settings,
    )
    search = SearchService(fake_qdrant, settings)
    documents = DocumentsService(fake_qdrant)
    library = LibraryService(fake_qdrant, registry)
    return SimpleNS(
        ingestion=ingestion,
        search=search,
        documents=documents,
        library=library,
        jobs=jobs,
        registry=registry,
        qdrant=fake_qdrant,
        ollama=fake_ollama,
    )


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestIngest:
    def test_happy_path(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h, job_id="j1")

        assert res["nodes"] > 0
        assert res["name"] == "ТЕСТ 1"
        assert wired.qdrant.points, "points must be upserted to qdrant"
        assert wired.jobs.get("j1")["status"] == "done"
        assert wired.registry.has_hash(h)
        assert res["version"] in wired.registry.versions(res["name"])

    def test_version_override_wins(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, version_override="Ред. 99"
        )
        assert res["version"].startswith("Ред. 99")

    def test_same_version_string_different_text_is_disambiguated(
        self, wired, sample_raw
    ):
        h1 = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h1)

        other = sample_raw + [
            {"text": "Дополнительный абзац.", "category": "NarrativeText", "html": None}
        ]
        h2 = DocumentParser.content_hash(other)
        res2 = wired.ingestion.ingest("doc2.docx", other, h2)

        # version string collides -> second ingest gets a hash suffix and lists the other version
        assert res2["version"] != "ТЕСТ 1 ред. 1"
        assert "ТЕСТ 1 ред. 1" in res2["other_versions"]

    def test_failure_sets_error_status_and_reraises(
        self, wired, sample_raw, monkeypatch
    ):
        monkeypatch.setattr(
            wired.qdrant,
            "upsert",
            lambda points: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        h = DocumentParser.content_hash(sample_raw)
        with pytest.raises(RuntimeError):
            wired.ingestion.ingest("doc.docx", sample_raw, h, job_id="jerr")
        assert wired.jobs.get("jerr")["status"] == "error"

    def test_references_attached_and_pending_registered(
        self, wired, sample_raw, monkeypatch
    ):
        from src.dvd_service.modules.reference_patterns import normalize_designation

        # Force the extractor to emit one external reference to a not-yet-loaded document.
        monkeypatch.setattr(
            wired.ingestion.reference_extractor,
            "extract",
            lambda nodes, client: {
                nodes[1]["id"]: [
                    {
                        "raw": "ГОСТ 9999, п. 5.1",
                        "target_name": "ГОСТ 9999",
                        "target_numbering": "5.1",
                    }
                ]
            },
        )
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)

        refs = [
            r
            for _v, pl in wired.qdrant.points.values()
            for r in pl.get("references", [])
        ]
        assert any(
            r["target_name"] == "ГОСТ 9999" and r["resolved"] is False for r in refs
        )
        assert wired.registry.peek_pending(normalize_designation("ГОСТ 9999"))

    def test_reference_linking_disabled_skips_stage(
        self, settings, sample_raw, fake_ollama, fake_qdrant, fake_redis, monkeypatch
    ):
        monkeypatch.setattr(svc, "OllamaClient", lambda *a, **k: fake_ollama)
        s = settings.model_copy(update={"enable_reference_linking": False})
        redis_client = RedisClient(s)
        ingestion = IngestionService(
            DocumentParser(s),
            StructureTagger(s),
            HierarchyBuilder(),
            Tagger(s),
            VersionDetector(),
            ReferenceExtractor(s),
            ReferenceResolver(fake_qdrant, DocumentRegistry(redis_client), s),
            fake_qdrant,
            DocumentRegistry(redis_client),
            JobStore(redis_client),
            s,
        )
        called = {"v": False}
        monkeypatch.setattr(
            ingestion.reference_extractor,
            "extract",
            lambda *a, **k: called.__setitem__("v", True) or {},
        )
        h = DocumentParser.content_hash(sample_raw)
        ingestion.ingest("doc.docx", sample_raw, h)
        assert called["v"] is False  # extraction stage skipped when the flag is off

    def test_repr(self, wired):
        assert repr(wired.ingestion).startswith("IngestionService(")


class TestSearch:
    def test_search_returns_hits_after_ingest(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        resp = wired.search.search(SearchRequest(query="требования", limit=5), None)
        assert resp.count >= 1
        assert resp.hits[0].text

    def test_context_height_expands_neighbours(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        resp = wired.search.search(
            SearchRequest(query="требования", limit=1, context_height=2), None
        )
        assert resp.hits[0].context is not None

    def test_build_filter_combines_conditions(self, wired):
        req = SearchRequest(query="q", name="СП 1", version="v1", tags=["a", "b"])
        flt = wired.search._build_filter(req, kind="text")
        assert flt is not None and len(flt.must) == 4  # kind + name + version + tags

    def test_build_filter_with_block_and_types(self, wired):
        req = SearchRequest(query="q", block="amendment", types=["clause", "subclause"])
        flt = wired.search._build_filter(req, kind=None)
        assert flt is not None and len(flt.must) == 2  # block + types

    def test_build_filter_none_when_no_constraints(self, wired):
        assert wired.search._build_filter(SearchRequest(query="q"), kind=None) is None

    def test_repr(self, wired):
        assert repr(wired.search).startswith("SearchService(")


class TestDocumentsService:
    def test_lists_ingested_document_with_aggregated_metadata(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)

        resp = wired.documents.list_documents()
        assert resp.count == 1
        doc = resp.documents[0]
        assert doc.name == res["name"] and doc.version == res["version"]
        assert doc.node_count == res["nodes"]
        assert doc.blocks == ["main"]
        assert doc.uploaded_at  # populated by ingest()

    def test_filters_by_name(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        assert wired.documents.list_documents(name="nope").count == 0
        assert wired.documents.list_documents(name="ТЕСТ 1").count == 1

    def test_filters_by_uploaded_range_excludes_out_of_range(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        future = "2999-01-01T00:00:00+00:00"
        assert wired.documents.list_documents(uploaded_from=future).count == 0
        assert wired.documents.list_documents(uploaded_to=future).count == 1

    def test_empty_store_returns_no_documents(self, wired):
        assert wired.documents.list_documents().count == 0

    def test_repr(self, wired):
        assert repr(wired.documents).startswith("DocumentsService(")


class TestGeneralPurposeFields:
    def test_payload_carries_identity_grounding_and_provenance(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            h,
            doc_type="regulation",
            corpus="norms",
            lang="ru",
            external_ids={"code": "СП 99.99999.2099"},
        )
        # inspect any stored point payload
        _vec, payload = next(iter(wired.qdrant.points.values()))
        assert payload["doc_id"] == res["doc_id"]
        assert payload["doc_type"] == "regulation" and payload["corpus"] == "norms"
        assert payload["lang"] == "ru"
        assert payload["external_ids"] == {"code": "СП 99.99999.2099"}
        assert payload["version_id"] and payload["version_id"].endswith(h[:12])
        assert "сп_99_99999_2099" in payload["lookup_keys"]
        assert payload["parser_version"] and payload["embedding_meta"]["dim"] == 1024
        # a content-bearing fragment is grounded back to the source text
        grounded = [
            p
            for _v, p in wired.qdrant.points.values()
            if p.get("char_start") is not None
        ]
        assert grounded, "at least one fragment must carry source offsets"
        g = grounded[0]
        assert g["char_end"] > g["char_start"] and g["span_id"]


class TestLibrary:
    def test_list_and_get_document(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, external_ids={"code": "X-1"}
        )

        listing = wired.library.list_documents()
        assert listing.count == 1
        assert listing.documents[0].doc_id == res["doc_id"]

        detail = wired.library.get_document(res["doc_id"])
        assert detail is not None
        assert detail.text  # assembled in reading order
        assert detail.fragments and detail.fragments[0].id
        # fragments are returned in document reading order
        orders = [f.order for f in detail.fragments]
        assert orders == sorted(orders)

    def test_get_missing_document_returns_none(self, wired):
        assert wired.library.get_document("nope") is None

    def test_find_by_external_id(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, external_ids={"code": "X-1"}
        )
        found = wired.library.find_documents("X-1")
        assert found.count == 1 and found.documents[0].doc_id == res["doc_id"]

    def test_repr(self, wired):
        assert repr(wired.library).startswith("LibraryService(")
