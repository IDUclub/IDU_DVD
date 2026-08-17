"""Unit tests for src/dvd_service/services/dvd_service — IngestionService and SearchService.

Wires the *real* pipeline modules (parser/structure/hierarchy/version) but fakes the
external boundaries: LLM (FakeOllama), Qdrant (FakeQdrantRepo) and Redis (fakeredis). This
exercises the full ingest+search orchestration without any live service.

Covers: end-to-end ingest (job status, registry, upsert), version override, version collision
resolution, error handling, search filtering, and context expansion.
"""

from __future__ import annotations

import pytest

import src.dvd_service.services.dvd_service as svc
from src.api_clients import COUNTRY_TERRITORY_ID
from src.broker.outbox import EventOutbox
from src.common.db.redis_client import DocumentRegistry, JobStore, RedisClient
from src.dvd_service.dto import SearchRequest
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.modules.tagging import VersionDetector
from src.dvd_service.modules.territory import TerritoryResolver
from src.dvd_service.services.dvd_service import (
    DocumentEditorService,
    DocumentsService,
    IngestionService,
    LibraryService,
    SearchService,
    TagsService,
)


@pytest.fixture
def wired(
    settings,
    fake_ollama,
    fake_qdrant,
    fake_redis,
    fake_document_storage,
    user_index_registry,
    monkeypatch,
):
    """Build IngestionService + SearchService with real modules and faked boundaries."""
    monkeypatch.setattr(svc, "create_llm", lambda *a, **k: fake_ollama)
    monkeypatch.setattr(svc, "create_embedder", lambda *a, **k: fake_ollama)
    redis_client = RedisClient(settings)
    jobs = JobStore(redis_client)
    registry = DocumentRegistry(redis_client)
    outbox = EventOutbox(redis_client, settings)
    ingestion = IngestionService(
        DocumentParser(settings),
        StructureTagger(settings),
        HierarchyBuilder(),
        VersionDetector(),
        ReferenceExtractor(settings),
        ReferenceResolver(fake_qdrant, registry, settings),
        fake_qdrant,
        registry,
        fake_document_storage,
        jobs,
        settings,
        outbox=outbox,
    )

    class FakeUrbanApi:
        def project_id_for_scenario(self, _scenario_id):
            return "p1"

    search = SearchService(
        fake_qdrant,
        settings,
        user_index_registry,
        urban_api=FakeUrbanApi(),
    )
    documents = DocumentsService(fake_qdrant)
    library = LibraryService(fake_qdrant, registry)
    editor = DocumentEditorService(fake_qdrant, registry, settings)
    tags = TagsService(fake_qdrant)
    return SimpleNS(
        ingestion=ingestion,
        search=search,
        documents=documents,
        library=library,
        editor=editor,
        tags=tags,
        jobs=jobs,
        registry=registry,
        outbox=outbox,
        qdrant=fake_qdrant,
        storage=fake_document_storage,
        ollama=fake_ollama,
        user_index_registry=user_index_registry,
    )


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def wired_with_territory(wired):
    """The same pipeline, with a territory resolver over a faked Urban API attached."""
    from unit.test_territory import FakeUrbanApi

    def _attach(broken: bool = False):
        resolver = TerritoryResolver(FakeUrbanApi(broken=broken))
        wired.ingestion.territory = resolver
        wired.search.territory = resolver
        wired.documents.territory = resolver
        wired.library.territory = resolver
        return wired

    return _attach


class TestAdministrativeScope:
    """The head hints reach the payload, and an Urban API outage never fails an ingest."""

    def test_scope_is_written_onto_every_fragment(
        self, wired_with_territory, sample_raw
    ):
        # the faked head pass reports a federal document (see pipeline_chat_handler)
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        payloads = [payload for _vec, payload in wired.qdrant.points.values()]
        assert payloads
        for payload in payloads:
            assert payload["document_level"] == "federal"
            assert payload["territory_id"] == COUNTRY_TERRITORY_ID
            assert payload["territory_name"] == "Россия"
            assert payload["territory_path"] == [COUNTRY_TERRITORY_ID]
            assert payload["territory_source"] == "auto"
            assert payload["tagging_status"] == "ok"

    def test_urban_api_outage_still_indexes_the_document_as_pending(
        self, wired_with_territory, sample_raw
    ):
        """The main degradation path: tagging fails, ingestion does not."""
        wired = wired_with_territory(broken=True)
        result = wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        assert result["nodes"] > 0
        _vec, payload = next(iter(wired.qdrant.points.values()))
        assert payload["tagging_status"] == "pending"
        assert payload["territory_id"] is None
        assert payload["territory_source"] == "unset"
        assert "Urban API недоступен" in payload["tagging_error"]

    def test_without_a_resolver_documents_are_indexed_untagged(self, wired, sample_raw):
        wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        _vec, payload = next(iter(wired.qdrant.points.values()))
        assert payload["tagging_status"] == "pending"
        assert payload["document_level"] is None


class TestManualScopeWins:
    """Automatic detection never overwrites a human's territory — on any write path."""

    def _payload(self, wired):
        _vec, payload = next(iter(wired.qdrant.points.values()))
        return payload

    def test_explicit_territory_overrides_detection(
        self, wired_with_territory, sample_raw
    ):
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            territory_id=54,  # the head pass would have said "federal"
        )
        payload = self._payload(wired)
        assert payload["territory_id"] == 54
        assert payload["document_level"] == "municipal"
        assert payload["territory_source"] == "manual"

    def test_manual_identity_and_territory_skip_the_llm_entirely(
        self, wired_with_territory, sample_raw
    ):
        """Nothing is left to ask the model, so the head pass is not run."""
        wired = wired_with_territory()
        before = len(wired.ollama.chat_calls)
        wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            name_override="СП 5.13130.2025",
            version_override="2025",
            territory_id=54,
        )
        head_calls = [
            call
            for call in wired.ollama.chat_calls[before:]
            if "level" in call[2].get("properties", {})
        ]
        assert head_calls == []

    def test_a_new_version_keeps_the_manual_territory(
        self, wired_with_territory, sample_raw
    ):
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            name_override="СП 1",
            territory_id=54,
        )
        updated = sample_raw + [
            {
                "text": "Новый пункт документа.",
                "category": "NarrativeText",
                "html": None,
            }
        ]
        wired.ingestion.update(
            "СП 1",
            "doc2.docx",
            updated,
            DocumentParser.content_hash(updated),
            version_override="2026",
        )
        for _vec, payload in wired.qdrant.points.values():
            assert payload["territory_id"] == 54
            assert payload["territory_source"] == "manual"

    def test_a_full_reload_keeps_the_manual_territory(
        self, wired_with_territory, sample_raw
    ):
        """PUT wipes the document first — the rescue before the delete is what saves the tag."""
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            name_override="СП 1",
            territory_id=54,
        )
        wired.ingestion.reload(
            "СП 1", "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        payload = self._payload(wired)
        assert payload["territory_id"] == 54
        assert payload["territory_source"] == "manual"

    def test_a_human_may_override_a_human(self, wired_with_territory, sample_raw):
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            name_override="СП 1",
            territory_id=54,
        )
        wired.ingestion.reload(
            "СП 1",
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            territory_id=1,
        )
        payload = self._payload(wired)
        assert payload["territory_id"] == 1
        assert payload["document_level"] == "regional"

    def test_editing_the_territory_rewrites_the_whole_scope(
        self, wired_with_territory, sample_raw
    ):
        """The admin panel edits one field; level, names and path follow from it."""
        from unit.test_territory import FakeUrbanApi

        wired = wired_with_territory()
        wired.editor.territory = TerritoryResolver(FakeUrbanApi())
        result = wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        wired.editor.update_document(result["doc_id"], {"territory_id": 1})
        payload = self._payload(wired)
        assert payload["territory_id"] == 1
        assert payload["document_level"] == "regional"
        assert payload["territory_name"] == "Ленинградская область"
        assert payload["territory_path"] == [COUNTRY_TERRITORY_ID, 1]
        assert payload["territory_source"] == "manual"

    def test_clearing_the_territory_hands_the_document_back_to_detection(
        self, wired_with_territory, sample_raw
    ):
        from unit.test_territory import FakeUrbanApi

        wired = wired_with_territory()
        wired.editor.territory = TerritoryResolver(FakeUrbanApi())
        result = wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            territory_id=54,
        )
        wired.editor.update_document(result["doc_id"], {"territory_id": None})
        payload = self._payload(wired)
        assert payload["territory_id"] is None
        assert payload["territory_source"] == "unset"
        assert payload["tagging_status"] == "pending"

    def test_an_outage_keeps_the_chosen_id_as_pending(
        self, wired_with_territory, sample_raw
    ):
        """The upload still succeeds; the backfill finishes resolving the chosen territory."""
        wired = wired_with_territory(broken=True)
        wired.ingestion.ingest(
            "doc.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            territory_id=54,
        )
        payload = self._payload(wired)
        assert payload["territory_id"] == 54
        assert payload["territory_source"] == "manual"
        assert payload["tagging_status"] == "pending"
        assert payload["document_level"] is None


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

    def test_progress_reported_through_stages(self, wired, sample_raw):
        seen = []
        phases = []
        orig = wired.jobs.update

        def spy(job_id, **fields):
            if "stage" in fields:
                seen.append((fields["stage"], fields["stage_index"]))
                if fields.get("phase"):
                    phases.append(fields["phase"])
            orig(job_id, **fields)

        wired.jobs.update = spy
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h, job_id="jp")

        stages = [s for s, _ in seen]
        # the full ordered pipeline is walked, ending on the final indexing stage
        assert stages[0] == "structure-markup"
        assert "embeddings" in stages
        assert stages[-1] == "indexing"
        # structure-markup reports its sub-phases with a per-request counter
        assert "boundaries" in phases
        final = wired.jobs.get("jp")
        assert final["status"] == "done"
        assert final["stage_index"] == final["stage_total"] == 7
        assert final["overall_progress"] == final["task_progress"] == 100

    def test_gpu_gate_serializes_concurrent_ingests(self, wired, sample_raw):
        # With ingest_concurrency=1 (default) the GPU-bound pipeline is serialized: two ingests
        # started at once must never be inside the pipeline body simultaneously. We instrument
        # the first in-gate stage (parser.to_logical_parts) to record peak overlap.
        import threading
        import time

        active = {"cur": 0, "max": 0}
        lock = threading.Lock()
        orig = wired.ingestion.parser.to_logical_parts

        def tracked(raw, client, on_progress=None):
            with lock:
                active["cur"] += 1
                active["max"] = max(active["max"], active["cur"])
            time.sleep(
                0.05
            )  # widen the window so an unguarded pipeline would overlap here
            try:
                return orig(raw, client, on_progress=on_progress)
            finally:
                with lock:
                    active["cur"] -= 1

        wired.ingestion.parser.to_logical_parts = tracked
        h = DocumentParser.content_hash(sample_raw)

        def run(i):
            wired.ingestion.ingest(f"doc{i}.docx", sample_raw, h, job_id=f"g{i}")

        threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert active["max"] == 1  # never two documents in the GPU section at once

    def test_document_processed_event_enqueued(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)

        entry = wired.outbox.peek()
        assert entry["model"] == "DocumentProcessed"
        assert entry["payload"] == {
            "document_name": res["name"],
            "user_id": None,
            "scenario_id": None,
        }
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
            lambda nodes, client, on_progress=None: {
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
        self,
        settings,
        sample_raw,
        fake_ollama,
        fake_qdrant,
        fake_redis,
        fake_document_storage,
        monkeypatch,
    ):
        monkeypatch.setattr(svc, "create_llm", lambda *a, **k: fake_ollama)
        monkeypatch.setattr(svc, "create_embedder", lambda *a, **k: fake_ollama)
        s = settings.model_copy(update={"enable_reference_linking": False})
        redis_client = RedisClient(s)
        ingestion = IngestionService(
            DocumentParser(s),
            StructureTagger(s),
            HierarchyBuilder(),
            VersionDetector(),
            ReferenceExtractor(s),
            ReferenceResolver(fake_qdrant, DocumentRegistry(redis_client), s),
            fake_qdrant,
            DocumentRegistry(redis_client),
            fake_document_storage,
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
        assert wired.jobs.get("j2")["overall_progress"] == 100
        assert wired.jobs.get("j2")["stage_index"] == 7

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
            "user_id": None,
            "scenario_id": None,
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
            "user_id": None,
            "scenario_id": None,
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
            "user_id": None,
            "scenario_id": None,
        }


class TestSourceFileStorage:
    def test_ingest_stamps_source_object_key_on_payload_and_registry(
        self, wired, sample_raw
    ):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, source_object_key="key-v1"
        )
        assert all(
            pl.get("source_object_key") == "key-v1"
            for _v, pl in wired.qdrant.points.values()
        )
        rec = wired.registry.get_document(res["doc_id"])
        assert rec["source_object_key"] == "key-v1"

    def test_delete_whole_document_removes_its_source_object(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, source_object_key="key-v1"
        )
        wired.ingestion.delete_document(res["name"])
        assert wired.storage.delete_calls == ["key-v1"]

    def test_delete_single_version_removes_only_that_versions_object(
        self, wired, sample_raw
    ):
        h1 = DocumentParser.content_hash(sample_raw)
        res1 = wired.ingestion.ingest(
            "doc.docx", sample_raw, h1, source_object_key="key-v1"
        )
        updated = sample_raw + [
            {
                "text": "Новый пункт документа.",
                "category": "NarrativeText",
                "html": None,
            }
        ]
        h2 = DocumentParser.content_hash(updated)
        wired.ingestion.update(
            res1["name"],
            "doc.docx",
            updated,
            h2,
            version_override="ред. 2",
            source_object_key="key-v2",
        )

        wired.ingestion.delete_document(res1["name"], version="ред. 2")

        assert wired.storage.delete_calls == ["key-v2"]

    def test_delete_all_versions_removes_every_distinct_object(self, wired, sample_raw):
        h1 = DocumentParser.content_hash(sample_raw)
        res1 = wired.ingestion.ingest(
            "doc.docx", sample_raw, h1, source_object_key="key-v1"
        )
        updated = sample_raw + [
            {
                "text": "Новый пункт документа.",
                "category": "NarrativeText",
                "html": None,
            }
        ]
        h2 = DocumentParser.content_hash(updated)
        wired.ingestion.update(
            res1["name"],
            "doc.docx",
            updated,
            h2,
            version_override="ред. 2",
            source_object_key="key-v2",
        )

        wired.ingestion.delete_document(res1["name"])

        assert set(wired.storage.delete_calls) == {"key-v1", "key-v2"}

    def test_delete_without_source_object_key_deletes_nothing(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)  # no source_object_key
        wired.ingestion.delete_document(res["name"])
        assert wired.storage.delete_calls == []


class TestBuildSourceUrl:
    def test_none_when_no_object_key(self):
        assert svc.build_source_url({}, "СП 1", "v1") is None

    def test_shared_document_link(self):
        url = svc.build_source_url({"source_object_key": "k"}, "СП 1", "v1")
        assert url == "/documents/%D0%A1%D0%9F%201/source?version=v1"

    def test_user_document_link(self):
        url = svc.build_source_url(
            {"source_object_key": "k", "user_id": "u1", "project_id": "p1"},
            "СП 1",
            "v1",
        )
        assert url == (
            "/user-documents/%D0%A1%D0%9F%201/source?user_id=u1&project_id=p1&version=v1"
        )


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
        assert wired.jobs.get("jr")["overall_progress"] == 100

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
            "user_id": None,
            "scenario_id": None,
        }

    def test_reload_of_absent_document_emits_processed(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.reload("Новый документ", "doc.docx", sample_raw, h)
        events = outbox_entries(wired.outbox)
        assert [e["model"] for e in events] == ["DocumentProcessed"]
        assert events[0]["payload"] == {
            "document_name": "Новый документ",
            "user_id": None,
            "scenario_id": None,
        }


class TestScopeFilters:
    """Level/territory are filterable, and a hit carries the scope back to the caller."""

    def _ingest(self, wired, sample_raw, name, territory_id):
        return wired.ingestion.ingest(
            "doc.docx",
            list(sample_raw),
            DocumentParser.content_hash(sample_raw) + name,
            name_override=name,
            territory_id=territory_id,
        )

    def test_filter_by_document_level(self, wired_with_territory, sample_raw):
        wired = wired_with_territory()
        self._ingest(wired, sample_raw, "СП федеральный", COUNTRY_TERRITORY_ID)
        self._ingest(wired, sample_raw, "ПЗЗ Выборга", 54)
        resp = wired.search.search(
            SearchRequest(query="требования", limit=50, document_level="municipal"),
            None,
        )
        assert resp.count >= 1
        assert {hit.name for hit in resp.hits} == {"ПЗЗ Выборга"}

    def test_territory_filter_matches_the_ancestor_chain(
        self, wired_with_territory, sample_raw
    ):
        """Asking for Vyborg also returns the federal documents in force there."""
        wired = wired_with_territory()
        self._ingest(wired, sample_raw, "СП федеральный", COUNTRY_TERRITORY_ID)
        self._ingest(wired, sample_raw, "ПЗЗ Выборга", 54)
        self._ingest(wired, sample_raw, "Закон Ленобласти", 1)
        resp = wired.search.search(
            SearchRequest(query="требования", limit=50, territory_ids=[54]), None
        )
        assert {hit.name for hit in resp.hits} == {
            "СП федеральный",
            "Закон Ленобласти",
            "ПЗЗ Выборга",
        }

    def test_a_sibling_territory_is_not_matched(self, wired_with_territory, sample_raw):
        wired = wired_with_territory()
        self._ingest(wired, sample_raw, "ПЗЗ Выборга", 54)
        resp = wired.search.search(
            SearchRequest(query="требования", limit=50, territory_ids=[3144]), None
        )
        assert resp.count == 0

    def test_search_hits_carry_the_scope(self, wired_with_territory, sample_raw):
        wired = wired_with_territory()
        self._ingest(wired, sample_raw, "ПЗЗ Выборга", 54)
        hit = wired.search.search(
            SearchRequest(query="требования", limit=1), None
        ).hits[0]
        assert hit.document_level == "municipal"
        assert hit.territory_name == "Выборгский муниципальный район"
        assert hit.territory_path == [COUNTRY_TERRITORY_ID, 1, 54]

    def test_document_listing_filters_and_reports_the_scope(
        self, wired_with_territory, sample_raw
    ):
        wired = wired_with_territory()
        self._ingest(wired, sample_raw, "ПЗЗ Выборга", 54)
        self._ingest(wired, sample_raw, "СП федеральный", COUNTRY_TERRITORY_ID)
        listing = wired.documents.list_documents(document_level="municipal")
        assert [d.name for d in listing.documents] == ["ПЗЗ Выборга"]
        assert listing.documents[0].territory_id == 54
        assert listing.documents[0].territory_source == "manual"

    def test_pending_documents_are_findable(self, wired_with_territory, sample_raw):
        """How the admin panel lists what still needs a human."""
        wired = wired_with_territory(broken=True)
        wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        listing = wired.documents.list_documents(tagging_status="pending")
        assert listing.count == 1


class TestSearch:
    def test_search_returns_hits_after_ingest(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        resp = wired.search.search(SearchRequest(query="требования", limit=5), None)
        assert resp.count >= 1
        assert resp.hits[0].text

    def test_search_hit_source_file_url_reflects_stored_object(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h, source_object_key="key-v1")
        resp = wired.search.search(SearchRequest(query="требования", limit=5), None)
        assert resp.hits[0].source_file_url is not None
        assert resp.hits[0].source_file_url.startswith("/documents/")

    def test_context_height_expands_neighbours(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        resp = wired.search.search(
            SearchRequest(query="требования", limit=1, context_height=2), None
        )
        assert resp.hits[0].context is not None

    def test_search_excludes_user_scoped_documents_by_default(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="user-pt",
                    vector=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    payload={
                        "doc_id": "u-doc",
                        "name": "User doc",
                        "version": "v1",
                        "kind": "text",
                        "type": "clause",
                        "text": "требования пользователя",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                )
            ]
        )
        resp = wired.search.search(SearchRequest(query="требования", limit=50), None)
        assert all(h.name != "User doc" for h in resp.hits)

    def test_search_combined_includes_shared_and_user_index(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="user-pt",
                    vector=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    payload={
                        "doc_id": "u-doc",
                        "name": "User doc",
                        "version": "v1",
                        "kind": "text",
                        "type": "clause",
                        "text": "требования пользователя",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                )
            ]
        )
        resp = wired.search.search(
            SearchRequest(query="требования", limit=50, user_id="u1", scenario_id="s1"),
            None,
        )
        names = {h.name for h in resp.hits}
        assert "User doc" in names and "ТЕСТ 1" in names

    def test_search_index_only_excludes_shared(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="user-pt",
                    vector=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    payload={
                        "doc_id": "u-doc",
                        "name": "User doc",
                        "version": "v1",
                        "kind": "text",
                        "type": "clause",
                        "text": "требования пользователя",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                )
            ]
        )
        resp = wired.search.search(
            SearchRequest(
                query="требования",
                limit=50,
                user_id="u1",
                scenario_id="s1",
                include_shared=False,
            ),
            None,
        )
        names = {h.name for h in resp.hits}
        assert names == {"User doc"}

    def test_scenarios_in_same_project_share_documents(self, wired):
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="parent-pt",
                    vector=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "Parent doc",
                        "version": "v1",
                        "kind": "text",
                        "type": "clause",
                        "text": "наследование сценария",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                )
            ]
        )
        wired.user_index_registry.create("u1", "s1", "p1")
        wired.user_index_registry.create("u1", "s2", "p1", parent_scenario_id="s1")

        resp = wired.search.search(
            SearchRequest(
                query="наследование",
                limit=50,
                user_id="u1",
                scenario_id="s2",
                include_shared=False,
            ),
            None,
        )
        assert {h.name for h in resp.hits} == {"Parent doc"}

        resp_no_inherit = wired.search.search(
            SearchRequest(
                query="наследование",
                limit=50,
                user_id="u1",
                scenario_id="s2",
                include_shared=False,
                include_inherited=False,
            ),
            None,
        )
        assert {h.name for h in resp_no_inherit.hits} == {"Parent doc"}

    def test_build_filter_combines_conditions(self, wired):
        req = SearchRequest(query="q", name="СП 1", version="v1", tags=["a", "b"])
        flt = wired.search._build_filter(req, kind="text")
        # kind + name + version + tags + default shared-only exclusion
        assert flt is not None and len(flt.must) == 5

    def test_build_filter_with_block_and_types(self, wired):
        req = SearchRequest(query="q", block="amendment", types=["clause", "subclause"])
        flt = wired.search._build_filter(req, kind=None)
        assert flt is not None and len(flt.must) == 3  # block + types + shared-only

    def test_build_filter_document_names(self, wired):
        req = SearchRequest(query="q", document_names=["СП 1", "СП 2"])
        flt = wired.search._build_filter(req, kind=None)
        assert flt is not None and len(flt.must) == 2  # document_names + shared-only
        cond = flt.must[0]
        assert cond.key == "name" and set(cond.match.any) == {"СП 1", "СП 2"}

    def test_build_filter_none_when_no_constraints(self, wired):
        # No longer None: a default filter always excludes user-scoped documents so
        # unscoped callers keep seeing only the shared/regular document corpus.
        flt = wired.search._build_filter(SearchRequest(query="q"), kind=None)
        assert flt is not None and len(flt.must) == 1
        assert flt.must[0].is_empty.key == "user_id"

    def test_build_filter_user_scope_combined(self, wired):
        req = SearchRequest(query="q", user_id="u1", scenario_id="s1")
        flt = wired.search._build_filter(req, kind=None)
        assert flt.should is not None and len(flt.should) == 2

    def test_build_filter_user_scope_index_only(self, wired):
        req = SearchRequest(
            query="q", user_id="u1", scenario_id="s1", include_shared=False
        )
        flt = wired.search._build_filter(req, kind=None)
        assert flt.should is None
        keys = {c.key for c in flt.must}
        assert keys == {"user_id", "project_id"}

    def test_build_filter_requires_user_id_and_project_or_scenario(self):
        with pytest.raises(ValueError):
            SearchRequest(query="q", user_id="u1")

        req = SearchRequest(query="q", user_id="u1", project_id="p1")
        assert req.project_id == "p1"

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

    def test_source_file_url_populated_when_object_key_present(self, wired, sample_raw):
        from urllib.parse import quote

        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h, source_object_key="key-v1")
        doc = wired.documents.list_documents().documents[0]
        assert doc.source_file_url == (
            f"/documents/%D0%A2%D0%95%D0%A1%D0%A2%201/source"
            f"?version={quote(doc.version, safe='')}"
        )

    def test_source_file_url_none_without_object_key(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        doc = wired.documents.list_documents().documents[0]
        assert doc.source_file_url is None

    def test_default_listing_excludes_user_scoped_documents(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        wired.ingestion.ingest("doc.docx", sample_raw, h)
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="user-pt",
                    vector=[0.0],
                    payload={
                        "doc_id": "u-doc",
                        "name": "User doc",
                        "version": "v1",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                )
            ]
        )
        names = {d.name for d in wired.documents.list_documents().documents}
        assert names == {"ТЕСТ 1"}  # the user-scoped point never leaks in


class TestDocumentsServiceUserScope:
    def test_scoped_listing_returns_only_matching_index(self, wired):
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="p1",
                    vector=[0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "Own doc",
                        "version": "v1",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                ),
                PointStruct(
                    id="p2",
                    vector=[0.0],
                    payload={
                        "doc_id": "d2",
                        "name": "Other user doc",
                        "version": "v1",
                        "user_id": "OTHER",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                ),
            ]
        )
        resp = wired.documents.list_documents(user_id="u1", project_ids=["p1"])
        assert {d.name for d in resp.documents} == {"Own doc"}
        assert resp.documents[0].scenario_id == "s1"

    def test_scoped_listing_includes_all_documents_in_project(self, wired):
        from qdrant_client.models import PointStruct

        wired.qdrant.upsert(
            [
                PointStruct(
                    id="parent-pt",
                    vector=[0.0],
                    payload={
                        "doc_id": "d1",
                        "name": "Parent doc",
                        "version": "v1",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s1",
                    },
                ),
                PointStruct(
                    id="child-pt",
                    vector=[0.0],
                    payload={
                        "doc_id": "d2",
                        "name": "Child doc",
                        "version": "v1",
                        "user_id": "u1",
                        "project_id": "p1",
                        "scenario_id": "s2",
                    },
                ),
            ]
        )
        resp = wired.documents.list_documents(user_id="u1", project_ids=["p1"])
        assert {d.name for d in resp.documents} == {"Parent doc", "Child doc"}


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

    def test_get_document_surfaces_references(self, wired, sample_raw, monkeypatch):
        # Force one external reference onto a fragment, then read the whole document back
        # and assert the reference is surfaced on the fragment (not dropped by the DTO).
        monkeypatch.setattr(
            wired.ingestion.reference_extractor,
            "extract",
            lambda nodes, client, on_progress=None: {
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
        res = wired.ingestion.ingest("doc.docx", sample_raw, h)

        detail = wired.library.get_document(res["doc_id"])
        all_refs = [r for f in detail.fragments for r in f.references]
        assert any(r.target_name == "ГОСТ 9999" for r in all_refs)

    def test_find_by_external_id(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, external_ids={"code": "X-1"}
        )
        found = wired.library.find_documents("X-1")
        assert found.count == 1 and found.documents[0].doc_id == res["doc_id"]

    def test_repr(self, wired):
        assert repr(wired.library).startswith("LibraryService(")

    def test_source_file_url_populated_from_registry_record(self, wired, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        res = wired.ingestion.ingest(
            "doc.docx", sample_raw, h, source_object_key="key-v1"
        )
        summary = wired.library.list_documents().documents[0]
        assert summary.doc_id == res["doc_id"]
        assert summary.source_file_url is not None
        assert summary.source_file_url.startswith("/documents/")
        assert wired.library.get_document(res["doc_id"]).source_file_url is not None


class TestDocumentEditor:
    def test_updates_document_metadata_and_all_fragment_tags(self, wired, sample_raw):
        result = wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        response = wired.editor.update_document(
            result["doc_id"],
            {
                "title": "Ручной заголовок",
                "tags": ["проверено"],
                "metadata": {"owner": "admin"},
                "external_ids": {"code": "MANUAL-1"},
            },
        )
        assert response.points_updated == result["nodes"]
        payloads = wired.qdrant.list_by_doc(result["doc_id"])
        assert all(p["title"] == "Ручной заголовок" for p in payloads)
        assert all(p["tags"] == ["проверено"] for p in payloads)
        assert "MANUAL-1" in payloads[0]["lookup_keys"]
        assert wired.registry.get_document(result["doc_id"])["metadata"] == {
            "owner": "admin"
        }

    def test_text_edit_reembeds_fragment(self, wired, sample_raw):
        result = wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        fragment_id = next(iter(wired.qdrant.points))
        old_vector = wired.qdrant.points[fragment_id][0]
        # The hermetic fake intentionally uses tiny vectors; align the configured dimension
        # with that fake while retaining the production mismatch guard in the service.
        wired.editor.settings.vector_size = len(old_vector)
        updated = wired.editor.update_fragment(
            result["doc_id"], fragment_id, {"text": "Новый ручной текст"}
        )
        new_vector, payload = wired.qdrant.points[fragment_id]
        assert updated["text"] == payload["text"] == "Новый ручной текст"
        assert (
            new_vector is not old_vector
            and len(new_vector) == wired.editor.settings.vector_size
        )

    def test_rejects_fragment_from_another_document(self, wired, sample_raw):
        wired.ingestion.ingest(
            "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        fragment_id = next(iter(wired.qdrant.points))
        with pytest.raises(KeyError):
            wired.editor.update_fragment("other-doc", fragment_id, {"tags": ["x"]})


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


class TestGetNode:
    """Node-level reads: the step between a search hit and get_document that was missing.

    A hit already carries parent_id/child_ids/prev_id/next_id, but acting on them used to
    mean fetching every fragment of the document.
    """

    @staticmethod
    def _library(payloads: dict[str, dict]):
        from src.dvd_service.services.dvd_service import LibraryService

        class _Qdrant:
            def retrieve(self, ids):
                return {i: payloads[i] for i in ids if i in payloads}

        return LibraryService(_Qdrant(), registry=None)

    def test_returns_none_for_an_unknown_node(self):
        assert self._library({}).get_node("nope") is None

    def test_resolves_parent_children_and_neighbours(self):
        payloads = {
            "row2": {
                "doc_id": "d1",
                "name": "СП 42",
                "version": "2016",
                "type": "row",
                "text": "row two",
                "parent_id": "tbl",
                "child_ids": [],
                "prev_id": "row1",
                "next_id": "row3",
                "order": 2,
            },
            "tbl": {
                "type": "table",
                "kind": "table",
                "text": "Таблица 11.2",
                "table_html": "<table>full</table>",
                "order": 0,
            },
            "row1": {"type": "row", "text": "row one", "order": 1},
            "row3": {"type": "row", "text": "row three", "order": 3},
        }

        node = self._library(payloads).get_node("row2")

        assert node.id == "row2"
        assert node.doc_id == "d1"
        assert node.parent.id == "tbl"
        # the whole table comes back with the parent, however its rows were chunked
        assert node.parent.table_html == "<table>full</table>"
        assert node.prev.text == "row one"
        assert node.next.text == "row three"

    def test_children_come_back_in_reading_order(self):
        payloads = {
            "tbl": {"type": "table", "text": "t", "child_ids": ["c3", "c1", "c2"]},
            "c1": {"type": "row", "text": "one", "order": 1},
            "c2": {"type": "row", "text": "two", "order": 2},
            "c3": {"type": "row", "text": "three", "order": 3},
        }

        node = self._library(payloads).get_node("tbl")

        assert [c.text for c in node.children] == ["one", "two", "three"]

    def test_relatives_can_be_skipped(self):
        payloads = {
            "n": {
                "type": "clause",
                "text": "t",
                "child_ids": ["c1"],
                "prev_id": "p",
                "next_id": "x",
            },
            "c1": {"type": "row", "text": "child", "order": 1},
            "p": {"type": "clause", "text": "prev"},
            "x": {"type": "clause", "text": "next"},
        }

        node = self._library(payloads).get_node(
            "n", with_children=False, with_neighbours=False
        )

        assert node.children == []
        assert node.prev is None and node.next is None

    def test_survives_a_dangling_relative_id(self):
        """Deleted or user-scoped siblings shouldn't 500 the whole read."""
        payloads = {
            "n": {
                "type": "clause",
                "text": "t",
                "child_ids": ["gone"],
                "parent_id": "also-gone",
            }
        }

        node = self._library(payloads).get_node("n")

        assert node.children == []
        assert node.parent is None


class TestSearchWithinNode:
    """parent_id scopes a search to one node's children — drill into a table or clause
    instead of searching the whole corpus and hoping the right rows rank."""

    @staticmethod
    def _filter_for(**kwargs):
        from src.dvd_service.dto import SearchRequest
        from src.dvd_service.services.dvd_service import SearchService

        req = SearchRequest(query="q", **kwargs)
        service = SearchService.__new__(SearchService)
        service.territory = None
        return service._build_filter(req, None)

    @staticmethod
    def _keys(flt):
        return [c.key for c in flt.must if hasattr(c, "key")]

    def test_parent_id_becomes_a_filter_condition(self):
        flt = self._filter_for(parent_id="tbl-1")

        assert "parent_id" in self._keys(flt)

    def test_absent_parent_id_adds_no_condition(self):
        flt = self._filter_for()

        assert "parent_id" not in self._keys(flt)

    def test_combines_with_tags_and_doc_id(self):
        """Scoping to a table and filtering by tag are independent, and compose."""
        flt = self._filter_for(parent_id="tbl-1", tags=["нормативы"], doc_id="d1")

        keys = self._keys(flt)
        assert {"parent_id", "tags", "doc_id"} <= set(keys)


class TestDocumentScopes:
    """get_scopes reports what the corpus actually holds — the values worth filtering by."""

    def test_lists_levels_territories_and_pending_count(
        self, wired_with_territory, sample_raw
    ):
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "a.docx",
            list(sample_raw),
            DocumentParser.content_hash(sample_raw) + "a",
            name_override="ПЗЗ Выборга",
            territory_id=54,
        )
        wired.ingestion.ingest(
            "b.docx",
            list(sample_raw),
            DocumentParser.content_hash(sample_raw) + "b",
            name_override="СП 1",
            territory_id=COUNTRY_TERRITORY_ID,
        )
        scopes = wired.tags.get_scopes()
        assert scopes.levels == ["federal", "municipal"]
        assert {t.territory_id for t in scopes.territories} == {
            COUNTRY_TERRITORY_ID,
            54,
        }
        assert all(t.document_count == 1 for t in scopes.territories)
        assert scopes.pending_documents == 0

    def test_counts_documents_not_fragments(self, wired_with_territory, sample_raw):
        wired = wired_with_territory()
        wired.ingestion.ingest(
            "a.docx",
            sample_raw,
            DocumentParser.content_hash(sample_raw),
            territory_id=54,
        )
        territory = wired.tags.get_scopes().territories[0]
        assert territory.document_count == 1  # the document has several fragments

    def test_pending_documents_are_counted(self, wired_with_territory, sample_raw):
        wired = wired_with_territory(broken=True)
        wired.ingestion.ingest(
            "a.docx", sample_raw, DocumentParser.content_hash(sample_raw)
        )
        assert wired.tags.get_scopes().pending_documents == 1
