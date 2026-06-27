"""Integration: reference linking against the live stack (Qdrant + Redis, and full pipeline).

Two layers:
  * ``TestReferenceLinkingStores`` — exercises ``ReferenceResolver`` over the *real* Qdrant and
    Redis (no Ollama): resolve an external link to a loaded clause, queue a dangling link to a
    missing document, then back-fill it once the target is ingested. Deterministic.
  * ``TestPipelineReferences`` — a smoke run of the full ``IngestionService`` (real Ollama LLM)
    asserting the references field is produced and surfaced through search without error.

Each test cleans up its Qdrant collection (``temp_collection``) and the Redis keys it created.
"""

from __future__ import annotations

import uuid

import pytest
from qdrant_client.models import PointStruct

from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry
from src.dvd_service.modules.reference_patterns import normalize_designation
from src.dvd_service.modules.references import ReferenceResolver

pytestmark = pytest.mark.integration


def _vec(dim: int, hot: int = 0) -> list[float]:
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


class TestReferenceLinkingStores:
    def test_resolve_external_and_backfill_on_live_stack(
        self, temp_collection, require_redis
    ):
        repo = QdrantRepository(temp_collection)
        repo.ensure_collection()
        registry = DocumentRegistry(require_redis)

        # unique designations so the shared Redis registry stays isolated/cleanable
        suffix = uuid.uuid4().hex[:8]
        target_name = f"ГОСТ ITEST-{suffix}"
        missing_name = f"СП ITEST-{suffix}"
        source_doc = f"src-{suffix}"
        target_doc = f"tgt-{suffix}"
        dim = temp_collection.vector_size

        resolver = ReferenceResolver(repo, registry, temp_collection)
        try:
            # --- a loaded target: its clause 7.5 lives in Qdrant + the name is registered
            tgt_node = str(uuid.uuid4())
            repo.upsert(
                [
                    PointStruct(
                        id=tgt_node,
                        vector=_vec(dim),
                        payload={
                            "name": target_name,
                            "numbering": "7.5",
                            "doc_id": target_doc,
                            "version": "v1",
                            "text": "clause 7.5",
                        },
                    )
                ]
            )
            registry.register("h-tgt", target_name, "v1", target_doc)

            # --- resolve: one link to the loaded target (pinpointed), one to a missing doc (pending)
            node_refs = {
                "src-node": [
                    {
                        "raw": f"{target_name}, п. 7.5",
                        "target_name": target_name,
                        "target_numbering": "7.5",
                    },
                    {
                        "raw": f"{missing_name}, п. 3.2",
                        "target_name": missing_name,
                        "target_numbering": "3.2",
                    },
                ]
            }
            resolved = resolver.resolve(source_doc, "СП Текущий", node_refs, {})["src-node"]
            loaded = next(r for r in resolved if r.target_name == target_name)
            missing = next(r for r in resolved if r.target_name == missing_name)

            assert loaded.resolved and loaded.target_node_id == tgt_node
            assert missing.resolved is False
            assert len(registry.peek_pending(normalize_designation(missing_name))) == 1

            # store the source node (with its references) so back-fill can update it
            src_node = str(uuid.uuid4())
            repo.upsert(
                [
                    PointStruct(
                        id=src_node,
                        vector=_vec(dim),
                        payload={
                            "name": "СП Текущий",
                            "doc_id": source_doc,
                            "references": [r.model_dump() for r in resolved],
                        },
                    )
                ]
            )
            # re-key the pending entry onto the real source node id
            registry.pop_pending(normalize_designation(missing_name))
            registry.add_pending(
                normalize_designation(missing_name),
                {
                    "source_doc_id": source_doc,
                    "source_node_id": src_node,
                    "raw": missing.raw,
                    "target_numbering": "3.2",
                },
            )

            # --- the missing document arrives -> back-fill links the dangling reference
            missing_node = str(uuid.uuid4())
            repo.upsert(
                [
                    PointStruct(
                        id=missing_node,
                        vector=_vec(dim),
                        payload={
                            "name": missing_name,
                            "numbering": "3.2",
                            "doc_id": "missing-doc",
                            "version": "v1",
                            "text": "clause 3.2",
                        },
                    )
                ]
            )
            updated = resolver.backfill(
                missing_name, "missing-doc", "v1", {"3.2": missing_node}
            )
            assert updated == 1

            stored = repo.retrieve([src_node])[src_node]["references"]
            backfilled = next(r for r in stored if r["target_name"] == missing_name)
            assert backfilled["resolved"] is True
            assert backfilled["target_node_id"] == missing_node
            assert backfilled["target_doc_id"] == "missing-doc"
        finally:
            for name in (target_name, missing_name):
                require_redis.r.srem("dvd:names", name)
                require_redis.r.delete(f"dvd:versions:{name}")
                require_redis.r.delete(f"dvd:pending_ref:{normalize_designation(name)}")
            require_redis.r.delete("dvd:hash:h-tgt")


class TestPipelineReferences:
    def test_ingest_produces_references_field(
        self, temp_collection, require_redis, require_ollama, reset_dependencies
    ):
        from src.dependencies import init_dependencies
        from src.dvd_service.dto import SearchRequest

        doc = [
            {
                "text": "СП 99.99999.2099 Тестовый свод правил. Общие положения.",
                "category": "Title",
                "html": None,
            },
            {"text": "1 Область применения", "category": "NarrativeText", "html": None},
            {
                "text": "Требования настоящего документа применяются в соответствии с "
                "ГОСТ 12.1.004-91, пункт 7.5.",
                "category": "NarrativeText",
                "html": None,
            },
        ]
        deps = init_dependencies(temp_collection)
        content_hash = deps.parser.content_hash(doc)
        job_id = f"itest-refs-{uuid.uuid4().hex[:8]}"
        result = None
        try:
            result = deps.ingestion.ingest(
                "itest_refs.docx", doc, content_hash, job_id=job_id
            )
            assert result["nodes"] > 0
            assert deps.jobs.get(job_id)["status"] == "done"

            # the references field is present on every hit (possibly empty — LLM-dependent)
            hits = deps.search.search(
                SearchRequest(query="требования применяются", limit=5), None
            )
            assert all(isinstance(h.references, list) for h in hits.hits)
        finally:
            require_redis.r.delete(f"dvd:job:{job_id}")
            require_redis.r.delete(f"dvd:hash:{content_hash}")
            if result:
                name = result["name"]
                require_redis.r.srem("dvd:names", name)
                require_redis.r.delete(f"dvd:versions:{name}")
