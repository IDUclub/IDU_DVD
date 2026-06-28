"""Unit tests for the reference extraction + linking stage.

Hermetic: the LLM is the conftest ``FakeOllama`` (programmable ``chat``), Qdrant is
``FakeQdrantRepo``, Redis is fakeredis. Covers designation/numbering normalization, LLM
extraction mapping, resolution (internal / external-loaded / external-missing), pending
registration, and the back-fill that completes dangling links once the target is ingested.
"""

from __future__ import annotations

import pytest

from src.common.db.redis_client import DocumentRegistry, RedisClient
from src.dvd_service.modules.reference_patterns import (
    DESIGNATION_RE,
    normalize_designation,
    normalize_numbering,
)
from src.dvd_service.modules.references import ReferenceExtractor, ReferenceResolver


class _StubLLM:
    """Minimal LLM double — the extractor only ever calls ``chat``."""

    def __init__(self, handler) -> None:
        self._handler = handler

    def chat(self, system: str, user: str, schema: dict) -> dict:
        return self._handler(system, user, schema)


# --------------------------------------------------------------------------------------
# normalization helpers
# --------------------------------------------------------------------------------------
class TestNormalization:
    def test_designation_collapses_space_and_case(self):
        assert normalize_designation("сп  42.13330.2016") == "СП 42.13330.2016"
        assert normalize_designation(" ГОСТ Р 21.1101-2013 ") == "ГОСТ Р 21.1101-2013"

    def test_designation_strips_trailing_punctuation(self):
        assert normalize_designation("СП 42.13330.2016.") == "СП 42.13330.2016"

    def test_numbering_extracts_dotted_digits(self):
        assert normalize_numbering("п. 7.5") == "7.5"
        assert normalize_numbering("пункт 4.2.1") == "4.2.1"
        assert normalize_numbering("6") == "6"

    def test_designation_regex_matches_common_forms(self):
        text = "согласно СП 42.13330.2016 и ГОСТ 12.1.004-91 применяются нормы"
        found = [m.group("name") for m in DESIGNATION_RE.finditer(text)]
        assert "СП 42.13330.2016" in found
        assert any(f.startswith("ГОСТ 12.1.004") for f in found)


# --------------------------------------------------------------------------------------
# extractor
# --------------------------------------------------------------------------------------
def _extract_handler(refs_by_index: dict[int, list[dict]]):
    """A FakeOllama chat handler that returns the given references for the reference schema."""

    def handler(system: str, user: str, schema: dict) -> dict:
        import re

        ids = [int(m) for m in re.findall(r"^\[(\d+)\]", user, re.M)]
        return {
            "items": [{"id": i, "references": refs_by_index.get(i, [])} for i in ids]
        }

    return handler


class TestExtractor:
    def test_extract_maps_local_index_to_node_id(self, settings):
        nodes = [
            {"id": "node-a", "text": "Общие положения."},
            {"id": "node-b", "text": "В соответствии с СП 42.13330.2016."},
        ]
        ref = {
            "raw": "СП 42.13330.2016",
            "target_name": "СП 42.13330.2016",
            "target_numbering": "",
        }
        ollama = _StubLLM(handler=_extract_handler({1: [ref]}))
        out = ReferenceExtractor(settings).extract(nodes, ollama)
        assert "node-b" in out and out["node-b"][0]["target_name"] == "СП 42.13330.2016"
        assert "node-a" not in out  # no references -> not in result

    def test_extract_skips_blank_raw(self, settings):
        nodes = [{"id": "n0", "text": "x"}]
        ollama = _StubLLM(
            handler=_extract_handler({0: [{"raw": "  ", "target_name": "X"}]})
        )
        assert ReferenceExtractor(settings).extract(nodes, ollama) == {}

    def test_extract_survives_window_error(self, settings):
        nodes = [{"id": "n0", "text": "x"}]

        def boom(system, user, schema):
            raise RuntimeError("llm down")

        ollama = _StubLLM(handler=boom)
        assert ReferenceExtractor(settings).extract(nodes, ollama) == {}


# --------------------------------------------------------------------------------------
# resolver
# --------------------------------------------------------------------------------------
@pytest.fixture
def registry(settings, fake_redis) -> DocumentRegistry:
    return DocumentRegistry(RedisClient(settings))


@pytest.fixture
def resolver(fake_qdrant, registry, settings) -> ReferenceResolver:
    return ReferenceResolver(fake_qdrant, registry, settings)


class TestResolveInternal:
    def test_internal_clause_resolves_to_local_node(self, resolver):
        refs = {
            "src": [{"raw": "см. п. 1.2", "target_name": "", "target_numbering": "1.2"}]
        }
        out = resolver.resolve("doc-1", "СП Текущий", refs, {"1.2": "local-node"})
        ref = out["src"][0]
        assert ref.scope == "internal"
        assert ref.resolved and ref.target_node_id == "local-node"
        assert ref.target_doc_id == "doc-1"

    def test_internal_whole_document_is_resolved(self, resolver):
        refs = {
            "src": [
                {
                    "raw": "в текущем документе",
                    "target_name": "",
                    "target_numbering": "",
                }
            ]
        }
        ref = resolver.resolve("doc-1", "СП Текущий", refs, {})["src"][0]
        assert ref.scope == "internal" and ref.resolved is True

    def test_internal_missing_clause_unresolved(self, resolver):
        refs = {
            "src": [{"raw": "см. п. 9.9", "target_name": "", "target_numbering": "9.9"}]
        }
        ref = resolver.resolve("doc-1", "СП Текущий", refs, {"1.2": "local"})["src"][0]
        assert ref.scope == "internal" and ref.resolved is False


class TestResolveExternal:
    def test_external_loaded_pinpoints_clause(self, resolver, fake_qdrant, registry):
        registry.register("h", "СП 42.13330.2016", "v1", "tgt-doc")
        fake_qdrant.points["tgt-node"] = (
            [0.0],
            {
                "name": "СП 42.13330.2016",
                "numbering": "7.5",
                "doc_id": "tgt-doc",
                "version": "v1",
            },
        )
        refs = {
            "src": [
                {
                    "raw": "СП 42.13330.2016, п. 7.5",
                    "target_name": "СП 42.13330.2016",
                    "target_numbering": "7.5",
                }
            ]
        }
        ref = resolver.resolve("doc-1", "СП Текущий", refs, {})["src"][0]
        assert ref.scope == "external" and ref.resolved is True
        assert ref.target_node_id == "tgt-node"
        assert ref.target_doc_id == "tgt-doc" and ref.target_version == "v1"

    def test_external_loaded_document_level_when_clause_absent(
        self, resolver, fake_qdrant, registry
    ):
        registry.register("h", "СП 42.13330.2016", "v1", "tgt-doc")
        fake_qdrant.points["title"] = (
            [0.0],
            {
                "name": "СП 42.13330.2016",
                "numbering": "",
                "doc_id": "tgt-doc",
                "version": "v1",
            },
        )
        refs = {
            "src": [
                {
                    "raw": "см. СП 42.13330.2016",
                    "target_name": "СП 42.13330.2016",
                    "target_numbering": "",
                }
            ]
        }
        ref = resolver.resolve("doc-1", "СП Текущий", refs, {})["src"][0]
        assert ref.resolved is True
        assert ref.target_doc_id == "tgt-doc"
        assert ref.target_node_id is None  # whole-document link, no clause pinpointed

    def test_external_missing_registers_pending(self, resolver, registry):
        refs = {
            "src": [
                {
                    "raw": "ГОСТ 9999, п. 5.1",
                    "target_name": "ГОСТ 9999",
                    "target_numbering": "5.1",
                }
            ]
        }
        ref = resolver.resolve("doc-1", "СП Текущий", refs, {})["src"][0]
        assert ref.resolved is False and ref.target_node_id is None
        pending = registry.peek_pending(normalize_designation("ГОСТ 9999"))
        assert len(pending) == 1
        assert pending[0]["source_node_id"] == "src"
        assert pending[0]["target_numbering"] == "5.1"


class TestBackfill:
    def test_backfill_completes_pending_reference(
        self, resolver, fake_qdrant, registry
    ):
        # 1) a document references a not-yet-loaded ГОСТ 9999 -> pending + unresolved ref stored
        refs = {
            "src": [
                {
                    "raw": "ГОСТ 9999, п. 5.1",
                    "target_name": "ГОСТ 9999",
                    "target_numbering": "5.1",
                }
            ]
        }
        resolved = resolver.resolve("src-doc", "СП Текущий", refs, {})
        fake_qdrant.points["src"] = (
            [0.0],
            {"name": "СП Текущий", "references": [resolved["src"][0].model_dump()]},
        )

        # 2) ГОСТ 9999 is ingested later — back-fill links the dangling reference to its clause
        updated = resolver.backfill(
            "ГОСТ 9999", "gost-doc", "ГОСТ 9999 ред.1", {"5.1": "gost-node"}
        )
        assert updated == 1

        stored = fake_qdrant.points["src"][1]["references"][0]
        assert stored["resolved"] is True
        assert stored["target_doc_id"] == "gost-doc"
        assert stored["target_version"] == "ГОСТ 9999 ред.1"
        assert stored["target_node_id"] == "gost-node"
        # pending queue is drained
        assert registry.peek_pending(normalize_designation("ГОСТ 9999")) == []

    def test_backfill_no_pending_is_noop(self, resolver):
        assert resolver.backfill("СП Нет", "d", "v", {}) == 0


class TestRepr:
    def test_extractor_repr(self, settings):
        assert repr(ReferenceExtractor(settings)).startswith("ReferenceExtractor(")

    def test_resolver_repr(self, resolver):
        assert repr(resolver) == "ReferenceResolver()"
