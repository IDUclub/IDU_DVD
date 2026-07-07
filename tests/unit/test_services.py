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
from src.broker.outbox import EventOutbox
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
    TagsService,
)


@pytest.fixture
def wired(settings, fake_ollama, fake_qdrant, fake_redis, monkeypatch):
    """Build IngestionService + SearchService with real modules and faked boundaries."""
    monkeypatch.setattr(svc, "OllamaClient", lambda *a, **k: fake_ollama)
    monkeypatch.setattr(svc, "create_embedder", lambda *a, **k: fake_ollama)
    redis_client = RedisClient(settings)
    jobs = JobStore(redis_client)
    registry = DocumentRegistry(redis_client)
    outbox = EventOutbox(redis_client, settings)
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
        outbox=outbox,
    )
    search = SearchService(fake_qdrant, settings)
    documents = DocumentsService(fake_qdrant)
    library = LibraryService(fake_qdrant, registry)
    tags = TagsService(fake_qdrant)
    return SimpleNS(
        ingestion=ingestion,
        search=search,
        documents=documents,
        library=library,
        tags=tags,
        jobs=jobs,
        registry=registry,
        outbox=outbox,
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

    def test_document_processed_event_enqueued(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)

        entry = wired.outbox.peek()
        assert entry["model"] == "DocumentProcessed"
        assert entry["payload"] == {"document_name": res["name"]}
        assert wired.outbox.size() == 1

    def test_no_event_without_outbox(self, wired, sample_raw):
        wired.ingestion.outbox = None
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        assert wired.outbox.size() == 0

    def test_failed_ingest_enqueues_no_event(self, wired, sample_raw, monkeypatch):
        monkeypatch.setattr(
            wired.qdrant,
            "upsert",
            lambda points: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        h = DocumentParser.content_hash(sample_raw)
        with pytest.raises(RuntimeError):
            wired.ingestion.ingest("doc.docx", sample_raw, h)
        assert wired.outbox.size() == 0

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
        monkeypatch.setattr(svc, "create_embedder", lambda *a, **k: fake_ollama)
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


def outbox_entries(outbox) -> list[dict]:
    """All queued Kafka events, oldest first (peek only exposes the head)."""
    import json

    return [json.loads(v) for v in outbox.r.lrange(outbox.key, 0, -1)]


def _version_detect_calls(ollama) -> list:
    """LLM calls that used the version-detection schema (top-level name+version props)."""
    return [
        c
        for c in ollama.chat_calls
        if {"name", "version"} <= set(c[2].get("properties", {}))
    ]


class TestManualIdentity:
    def test_name_override_with_4digit_version_skips_llm_detection(
        self, wired, sample_raw
    ):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, name_override="СП 5.13130.2025"
        )
        assert res["name"] == "СП 5.13130.2025"
        assert res["version"] == "2025"  # trailing 4-digit group of the name
        assert not _version_detect_calls(wired.ollama)

    def test_name_override_without_digits_falls_back_to_llm_version(
        self, wired, sample_raw
    ):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, name_override="Правила без года"
        )
        assert res["name"] == "Правила без года"
        assert (
            res["version"] == "ТЕСТ 1 ред. 1"
        )  # detector's version, detected name ignored
        assert _version_detect_calls(wired.ollama)

    def test_version_override_beats_4digit_extraction(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            h,
            version_override="ред. 7",
            name_override="СП 5.13130.2025",
        )
        assert res["version"] == "ред. 7"

    def test_fresh_ingest_tags_fragments_with_their_version(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)
        assert all(
            pl["versions"] == [res["version"]]
            for _v, pl in wired.qdrant.points.values()
        )


class TestBlockMatching:
    """The deterministic source-block diff matcher (``_match_by_blocks``) in isolation."""

    H1 = [f"h{i}" for i in range(3)]  # three source blocks

    @staticmethod
    def _pt(pid, blocks):
        return {"id": pid, "src_block_ids": blocks}

    @staticmethod
    def _node(nid, blocks):
        return {"id": nid, "src_ids": blocks}

    def test_only_truly_changed_block_is_reindexed(self):
        base = [self._pt("A", [0]), self._pt("B", [1]), self._pt("C", [2])]
        nodes = [self._node("x", [0]), self._node("y", [1]), self._node("z", [2])]
        new_hashes = ["h0", "CHANGED", "h2"]
        reuse, insert, id_map = IngestionService._match_by_blocks(
            base, nodes, self.H1, new_hashes
        )
        assert reuse == {"A", "C"} and insert == {"y"}
        assert id_map == {"x": "A", "z": "C"}

    def test_fragmentation_drift_over_unchanged_text_is_ignored(self):
        # The LLM merged two unchanged blocks into one fragment this time — no re-indexing.
        base = [self._pt("A", [0]), self._pt("B", [1])]
        nodes = [self._node("x", [0, 1])]
        reuse, insert, _ = IngestionService._match_by_blocks(
            base, nodes, ["h0", "h1"], ["h0", "h1"]
        )
        assert reuse == {"A", "B"} and insert == set()

    def test_fragment_straddling_an_edit_evicts_overlapping_reuse(self):
        # New fragment covers an unchanged block + an added one: it must be inserted, and
        # the old fragment of that unchanged block must not be reused (no double storage).
        base = [self._pt("A", [0]), self._pt("B", [1])]
        nodes = [self._node("x", [0]), self._node("y", [1, 2])]
        reuse, insert, id_map = IngestionService._match_by_blocks(
            base, nodes, ["h0", "h1"], ["h0", "h1", "ADDED"]
        )
        assert reuse == {"A"} and insert == {"y"}
        assert id_map == {"x": "A"}

    def test_inserted_shift_does_not_break_matching(self):
        # A block inserted in the middle shifts all following indices — diff must absorb it.
        base = [self._pt("A", [0]), self._pt("B", [1])]
        nodes = [self._node("x", [0]), self._node("n", [1]), self._node("y", [2])]
        reuse, insert, id_map = IngestionService._match_by_blocks(
            base, nodes, ["h0", "h1"], ["h0", "NEW", "h1"]
        )
        assert reuse == {"A", "B"} and insert == {"n"}
        assert id_map == {"x": "A", "y": "B"}

    def test_text_fallback_normalizes_whitespace(self):
        base = [{"id": "A", "text": "Пункт  1.1\nтребования."}]
        nodes = [{"id": "x", "text": "Пункт 1.1 требования."}]
        id_map, unmatched = IngestionService._match_by_text(base, nodes)
        assert id_map == {"x": "A"} and unmatched == []


class TestUpdateDocument:
    def _base(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        return wired.ingestion.ingest("doc.docx", sample_raw, h)

    def _updated_raw(self, sample_raw):
        return sample_raw + [
            {
                "text": "Новый пункт документа.",
                "category": "NarrativeText",
                "html": None,
            }
        ]

    def test_delta_update_tags_shared_and_inserts_new(self, wired, sample_raw):
        res1 = self._base(wired, sample_raw)
        updated = self._updated_raw(sample_raw)
        h2 = DocumentParser.content_hash(updated)
        res2 = wired.ingestion.update(
            res1["name"],
            "doc.docx",
            updated,
            h2,
            version_override="ред. 2",
            job_id="j2",
        )

        assert res2["reused_nodes"] > 0 and res2["new_nodes"] >= 1
        assert res2["nodes"] == res2["reused_nodes"] + res2["new_nodes"]
        payloads = [pl for _v, pl in wired.qdrant.points.values()]
        shared = [
            pl
            for pl in payloads
            if {res1["version"], "ред. 2"} <= set(pl.get("versions", []))
        ]
        assert shared, "unchanged fragments must carry both version tags"
        fresh = [pl for pl in payloads if pl.get("versions") == ["ред. 2"]]
        assert any(pl["text"] == "Новый пункт документа." for pl in fresh)
        # delta fragments join the same document structure
        assert all(pl["doc_id"] == res1["doc_id"] for pl in fresh)
        assert "ред. 2" in wired.registry.versions(res1["name"])
        assert wired.registry.has_hash(h2)
        assert wired.jobs.get("j2")["status"] == "done"
        assert wired.jobs.get("j2")["new_nodes"] == res2["new_nodes"]

    def test_update_emits_document_updated_event(self, wired, sample_raw):
        res1 = self._base(wired, sample_raw)
        updated = self._updated_raw(sample_raw)
        h2 = DocumentParser.content_hash(updated)
        res2 = wired.ingestion.update(
            res1["name"], "doc.docx", updated, h2, version_override="ред. 2"
        )
        events = outbox_entries(wired.outbox)
        assert [e["model"] for e in events] == ["DocumentProcessed", "DocumentUpdated"]
        assert events[1]["payload"] == {
            "document_name": res1["name"],
            "version": res2["version"],
        }

    def test_update_unknown_name_raises(self, wired):
        with pytest.raises(KeyError):
            wired.ingestion.update("нет такого", "doc.docx", [], "h-x")

    def test_list_documents_shows_both_versions(self, wired, sample_raw):
        res1 = self._base(wired, sample_raw)
        updated = self._updated_raw(sample_raw)
        h2 = DocumentParser.content_hash(updated)
        res2 = wired.ingestion.update(
            res1["name"], "doc.docx", updated, h2, version_override="ред. 2"
        )

        listed = {
            (d.name, d.version): d for d in wired.documents.list_documents().documents
        }
        assert (res1["name"], res1["version"]) in listed
        assert (res1["name"], "ред. 2") in listed
        # the new version is complete: shared fragments + the delta
        v2 = wired.documents.list_documents(version="ред. 2")
        assert v2.count == 1 and v2.documents[0].node_count == res2["nodes"]


class TestDeleteDocument:
    def test_delete_all_versions_wipes_store_and_registry(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)
        out = wired.ingestion.delete_document(res["name"])

        assert out["points_deleted"] > 0 and res["version"] in out["versions_removed"]
        assert wired.qdrant.points == {}
        assert wired.registry.versions(res["name"]) == []
        assert not wired.registry.has_name(res["name"])
        assert not wired.registry.has_hash(h)
        assert wired.registry.all_documents() == []

    def test_delete_single_version_keeps_shared_fragments(self, wired, sample_raw):
        h1 = DocumentParser.content_hash(sample_raw)
        res1 = wired.ingestion.ingest("doc.docx", sample_raw, h1)
        updated = sample_raw + [
            {
                "text": "Новый пункт документа.",
                "category": "NarrativeText",
                "html": None,
            }
        ]
        h2 = DocumentParser.content_hash(updated)
        res2 = wired.ingestion.update(
            res1["name"], "doc.docx", updated, h2, version_override="ред. 2"
        )

        before = len(wired.qdrant.points)
        out = wired.ingestion.delete_document(res1["name"], version="ред. 2")

        assert out["points_deleted"] == res2["new_nodes"]
        assert out["points_updated"] == res2["reused_nodes"]
        assert len(wired.qdrant.points) == before - res2["new_nodes"]
        assert all(
            "ред. 2" not in pl.get("versions", [])
            for _v, pl in wired.qdrant.points.values()
        )
        assert wired.registry.versions(res1["name"]) == [res1["version"]]
        assert wired.registry.has_hash(h1) and not wired.registry.has_hash(h2)

    def test_delete_unknown_name_raises(self, wired):
        with pytest.raises(KeyError):
            wired.ingestion.delete_document("нет такого")

    def test_delete_unknown_version_raises(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)
        with pytest.raises(KeyError):
            wired.ingestion.delete_document(res["name"], version="нет такой")

    def test_full_delete_emits_document_deleted_event(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)
        wired.ingestion.delete_document(res["name"])
        last = outbox_entries(wired.outbox)[-1]
        assert last["model"] == "DocumentDeleted"
        assert last["payload"] == {
            "document_name": res["name"],
            "versions_removed": [res["version"]],
            "document_removed": True,
        }

    def test_version_delete_emits_event_with_document_kept(self, wired, sample_raw):
        h1 = DocumentParser.content_hash(sample_raw)
        res1 = wired.ingestion.ingest("doc.docx", sample_raw, h1)
        updated = sample_raw + [
            {
                "text": "Новый пункт документа.",
                "category": "NarrativeText",
                "html": None,
            }
        ]
        h2 = DocumentParser.content_hash(updated)
        wired.ingestion.update(
            res1["name"], "doc.docx", updated, h2, version_override="ред. 2"
        )
        wired.ingestion.delete_document(res1["name"], version="ред. 2")
        last = outbox_entries(wired.outbox)[-1]
        assert last["model"] == "DocumentDeleted"
        assert last["payload"] == {
            "document_name": res1["name"],
            "versions_removed": ["ред. 2"],
            "document_removed": False,  # the 2020 edition is still stored
        }


class TestReloadDocument:
    def test_reload_replaces_all_versions(self, wired, sample_raw):
        h1 = DocumentParser.content_hash(sample_raw)
        res1 = wired.ingestion.ingest("doc.docx", sample_raw, h1)
        new_raw = sample_raw + [
            {
                "text": "Полностью новая редакция.",
                "category": "NarrativeText",
                "html": None,
            }
        ]
        h2 = DocumentParser.content_hash(new_raw)
        res2 = wired.ingestion.reload(
            res1["name"],
            "doc.docx",
            new_raw,
            h2,
            version_override="ред. 9",
            job_id="jr",
        )

        assert res2["name"] == res1["name"]  # identity pinned by the URL name
        assert wired.registry.versions(res1["name"]) == ["ред. 9"]
        assert not wired.registry.has_hash(h1) and wired.registry.has_hash(h2)
        assert all(
            pl["versions"] == ["ред. 9"] for _v, pl in wired.qdrant.points.values()
        )
        assert wired.jobs.get("jr")["status"] == "done"

    def test_reload_of_absent_document_acts_as_ingest(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.reload("Новый документ", "doc.docx", sample_raw, h)
        assert res["name"] == "Новый документ"
        assert wired.qdrant.points

    def test_reload_emits_single_updated_event(self, wired, sample_raw):
        h1 = DocumentParser.content_hash(sample_raw)
        res1 = wired.ingestion.ingest("doc.docx", sample_raw, h1)
        new_raw = sample_raw + [
            {"text": "Новая редакция.", "category": "NarrativeText", "html": None}
        ]
        h2 = DocumentParser.content_hash(new_raw)
        res2 = wired.ingestion.reload(
            res1["name"], "doc.docx", new_raw, h2, version_override="ред. 9"
        )
        events = outbox_entries(wired.outbox)
        # No intermediate DocumentDeleted — the replace is announced as one update.
        assert [e["model"] for e in events] == ["DocumentProcessed", "DocumentUpdated"]
        assert events[1]["payload"] == {
            "document_name": res1["name"],
            "version": res2["version"],
        }

    def test_reload_of_absent_document_emits_processed(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.reload("Новый документ", "doc.docx", sample_raw, h)
        events = outbox_entries(wired.outbox)
        assert [e["model"] for e in events] == ["DocumentProcessed"]
        assert events[0]["payload"] == {"document_name": "Новый документ"}


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

    def test_build_filter_document_names(self, wired):
        req = SearchRequest(query="q", document_names=["СП 1", "СП 2"])
        flt = wired.search._build_filter(req, kind=None)
        assert flt is not None and len(flt.must) == 1
        cond = flt.must[0]
        assert cond.key == "name" and set(cond.match.any) == {"СП 1", "СП 2"}

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
        assert payload["parser_version"] and payload["embedding_meta"]["dim"] == 2048
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


class TestTagsService:
    def test_returns_sorted_unique_tags(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        resp = wired.tags.get_tags()
        assert isinstance(resp.tags, list)
        assert resp.count == len(resp.tags)
        assert resp.tags == sorted(resp.tags)

    def test_deduplicates_tags_across_fragments(self, wired, fake_qdrant):
        fake_qdrant.points["a"] = ([0.1], {"tags": ["fire", "water"], "text": "x"})
        fake_qdrant.points["b"] = ([0.2], {"tags": ["fire", "earth"], "text": "y"})
        resp = wired.tags.get_tags()
        assert set(resp.tags) == {"earth", "fire", "water"}
        assert resp.count == 3

    def test_empty_store_returns_no_tags(self, wired):
        resp = wired.tags.get_tags()
        assert resp.count == 0 and resp.tags == []

    def test_repr(self, wired):
        assert repr(wired.tags).startswith("TagsService(")
