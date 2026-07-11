"""Unit tests for src/dvd_service/services/user_index_service — UserIndexService and the
per-request ``build_user_ingestion`` factory.

``UserIndexService`` runs against ``FakeQdrantRepo`` + a real ``UserIndexRegistry``/
``DocumentRegistry`` backed by fakeredis. ``build_user_ingestion`` is checked for correct wiring
(scoped registry prefix, ``ScopedQdrantRepository``, reference-linking disabled,
``ScopedEventOutbox``) without running the pipeline.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.broker.outbox import EventOutbox, ScopedEventOutbox
from src.common.db.qdrant_client import ScopedQdrantRepository
from src.common.db.redis_client import DocumentRegistry, RedisClient, UserIndexRegistry
from src.dvd_service.services.user_index_service import (
    UserIndexService,
    build_user_ingestion,
    build_user_ingestion_from_deps,
)


@pytest.fixture
def redis_client(settings, fake_redis) -> RedisClient:
    return RedisClient(settings)


@pytest.fixture
def index_registry(redis_client, settings) -> UserIndexRegistry:
    return UserIndexRegistry(redis_client, prefix=settings.registry_prefix)


@pytest.fixture
def real_outbox(redis_client, settings) -> EventOutbox:
    return EventOutbox(redis_client, settings)


@pytest.fixture
def service(
    fake_qdrant, redis_client, index_registry, settings, fake_document_storage
) -> UserIndexService:
    return UserIndexService(
        fake_qdrant,
        redis_client,
        index_registry,
        settings,
        storage=fake_document_storage,
    )


@pytest.fixture
def service_with_outbox(
    fake_qdrant,
    redis_client,
    index_registry,
    settings,
    fake_document_storage,
    real_outbox,
) -> UserIndexService:
    return UserIndexService(
        fake_qdrant,
        redis_client,
        index_registry,
        settings,
        storage=fake_document_storage,
        outbox=real_outbox,
    )


def _drain(outbox: EventOutbox) -> list[dict]:
    """Pop every queued entry off the outbox, decoded (model name + payload)."""
    entries = []
    while (entry := outbox.peek()) is not None:
        entries.append(entry)
        outbox.commit()
    return entries


class TestCreateIndex:
    def test_create_returns_zero_documents(self, service):
        info = service.create_index("u1", "s1", "p1")
        assert info.user_id == "u1" and info.scenario_id == "s1"
        assert info.project_id == "p1"
        assert info.document_count == 0

    def test_create_duplicate_raises(self, service):
        service.create_index("u1", "s1", "p1")
        with pytest.raises(ValueError):
            service.create_index("u1", "s1", "p1")

    def test_create_rejects_cycle(self, service):
        with pytest.raises(ValueError):
            service.create_index("u1", "s1", "p1", parent_scenario_id="s1")


class TestListIndices:
    def test_lists_with_document_counts(self, service, fake_qdrant):
        from qdrant_client.models import PointStruct

        service.create_index("u1", "s1", "p1")
        service.create_index("u1", "s2", "p1")
        fake_qdrant.upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
                )
            ]
        )
        listing = service.list_indices("u1")
        counts = {i.scenario_id: i.document_count for i in listing.indices}
        assert counts == {"s1": 1, "s2": 0}

    def test_empty_for_unknown_user(self, service):
        assert service.list_indices("ghost").count == 0


class TestDeleteIndex:
    def test_deletes_points_and_registry(self, service, fake_qdrant, index_registry):
        from qdrant_client.models import PointStruct

        service.create_index("u1", "s1", "p1")
        fake_qdrant.upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={"user_id": "u1", "scenario_id": "s1", "project_id": "p1"},
                ),
                PointStruct(
                    id="pt2",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "OTHER",
                        "project_id": "p1",
                    },
                ),
            ]
        )

        resp = service.delete_index("u1", "s1")

        assert resp.points_deleted == 1
        assert "pt1" not in fake_qdrant.points
        assert "pt2" in fake_qdrant.points  # a different scenario is untouched
        assert index_registry.get("u1", "s1") is None

    def test_deletes_distinct_source_objects(
        self, service, fake_qdrant, fake_document_storage
    ):
        from qdrant_client.models import PointStruct

        service.create_index("u1", "s1", "p1")
        fake_qdrant.upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "s1",
                        "project_id": "p1",
                        "source_object_key": "key-a",
                    },
                ),
                PointStruct(
                    id="pt2",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "s1",
                        "project_id": "p1",
                        "source_object_key": "key-b",
                    },
                ),
                PointStruct(
                    id="pt3",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "OTHER",
                        "project_id": "p1",
                        "source_object_key": "key-c",
                    },
                ),
            ]
        )

        service.delete_index("u1", "s1")

        assert set(fake_document_storage.delete_calls) == {"key-a", "key-b"}

    def test_wipes_the_scoped_document_registry(
        self, service, redis_client, index_registry, settings
    ):
        service.create_index("u1", "s1", "p1")
        scoped = DocumentRegistry(
            redis_client, prefix=f"{settings.registry_prefix}:user:u1:s1"
        )
        scoped.register("h1", "Doc", "v1", "d1")

        service.delete_index("u1", "s1")

        assert scoped.has_hash("h1") is False

    def test_delete_missing_index_raises(self, service):
        with pytest.raises(KeyError):
            service.delete_index("u1", "ghost")

    def test_no_outbox_no_event(self, service, fake_qdrant):
        # The default `service` fixture has no outbox — delete_index must not blow up.
        from qdrant_client.models import PointStruct

        service.create_index("u1", "s1", "p1")
        fake_qdrant.upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "s1",
                        "project_id": "p1",
                        "name": "Doc A",
                        "version": "v1",
                    },
                ),
            ]
        )
        service.delete_index("u1", "s1")  # must not raise

    def test_emits_scoped_document_deleted_per_name(
        self, service_with_outbox, fake_qdrant, real_outbox
    ):
        from qdrant_client.models import PointStruct

        service_with_outbox.create_index("u1", "s1", "p1")
        fake_qdrant.upsert(
            [
                PointStruct(
                    id="pt1",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "s1",
                        "project_id": "p1",
                        "name": "Doc A",
                        "version": "v1",
                    },
                ),
                PointStruct(
                    id="pt2",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "s1",
                        "project_id": "p1",
                        "name": "Doc A",
                        "version": "v2",
                    },
                ),
                PointStruct(
                    id="pt3",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "s1",
                        "project_id": "p1",
                        "name": "Doc B",
                        "version": "v1",
                    },
                ),
                # A different scenario's document — must not generate an event here.
                PointStruct(
                    id="pt4",
                    vector=[0.0],
                    payload={
                        "user_id": "u1",
                        "scenario_id": "OTHER",
                        "project_id": "p1",
                        "name": "Doc C",
                        "version": "v1",
                    },
                ),
            ]
        )

        service_with_outbox.delete_index("u1", "s1")

        entries = _drain(real_outbox)
        assert {e["model"] for e in entries} == {"DocumentDeleted"}
        by_name = {e["payload"]["document_name"]: e["payload"] for e in entries}
        assert set(by_name) == {"Doc A", "Doc B"}
        assert sorted(by_name["Doc A"]["versions_removed"]) == ["v1", "v2"]
        assert by_name["Doc A"]["document_removed"] is True
        # ScopedEventOutbox stamps user_id/scenario_id on every enqueued event.
        assert by_name["Doc A"]["user_id"] == "u1"
        assert by_name["Doc A"]["scenario_id"] == "s1"
        assert by_name["Doc B"]["versions_removed"] == ["v1"]

    def test_no_events_for_empty_index(self, service_with_outbox, real_outbox):
        service_with_outbox.create_index("u1", "s1", "p1")
        service_with_outbox.delete_index("u1", "s1")
        assert _drain(real_outbox) == []


class TestBuildUserIngestion:
    def test_scopes_registry_prefix(
        self, settings, fake_qdrant, redis_client, fake_document_storage
    ):
        ingestion = build_user_ingestion(
            settings=settings,
            qdrant=fake_qdrant,
            redis=redis_client,
            storage=fake_document_storage,
            jobs=SimpleNamespace(),
            parser=SimpleNamespace(),
            structure=SimpleNamespace(),
            hierarchy=SimpleNamespace(),
            version_detector=SimpleNamespace(),
            reference_extractor=SimpleNamespace(),
            reference_resolver=SimpleNamespace(),
            outbox=None,
            user_id="u1",
            project_id="p1",
            scenario_id="s1",
        )
        assert ingestion.registry.prefix == f"{settings.registry_prefix}:user:u1:s1"

    def test_wraps_qdrant_and_disables_reference_linking(
        self, settings, fake_qdrant, redis_client, fake_document_storage
    ):
        ingestion = build_user_ingestion(
            settings=settings,
            qdrant=fake_qdrant,
            redis=redis_client,
            storage=fake_document_storage,
            jobs=SimpleNamespace(),
            parser=SimpleNamespace(),
            structure=SimpleNamespace(),
            hierarchy=SimpleNamespace(),
            version_detector=SimpleNamespace(),
            reference_extractor=SimpleNamespace(),
            reference_resolver=SimpleNamespace(),
            outbox=None,
            user_id="u1",
            project_id="p1",
            scenario_id="s1",
        )
        assert isinstance(ingestion.qdrant, ScopedQdrantRepository)
        assert ingestion.settings.enable_reference_linking is False
        assert ingestion.outbox is None

    def test_wraps_outbox_when_given(
        self, settings, fake_qdrant, redis_client, fake_document_storage
    ):
        real_outbox = EventOutbox(redis_client, settings)
        ingestion = build_user_ingestion(
            settings=settings,
            qdrant=fake_qdrant,
            redis=redis_client,
            storage=fake_document_storage,
            jobs=SimpleNamespace(),
            parser=SimpleNamespace(),
            structure=SimpleNamespace(),
            hierarchy=SimpleNamespace(),
            version_detector=SimpleNamespace(),
            reference_extractor=SimpleNamespace(),
            reference_resolver=SimpleNamespace(),
            outbox=real_outbox,
            user_id="u1",
            project_id="p1",
            scenario_id="s1",
        )
        assert isinstance(ingestion.outbox, ScopedEventOutbox)

    def test_from_deps_reads_dependencies_container(
        self, settings, fake_qdrant, redis_client, fake_document_storage
    ):
        deps = SimpleNamespace(
            settings=settings,
            qdrant=fake_qdrant,
            redis=redis_client,
            user_document_storage=fake_document_storage,
            jobs=SimpleNamespace(),
            parser=SimpleNamespace(),
            structure=SimpleNamespace(),
            hierarchy=SimpleNamespace(),
            version_detector=SimpleNamespace(),
            reference_extractor=SimpleNamespace(),
            reference_resolver=SimpleNamespace(),
            outbox=None,
            publisher=SimpleNamespace(enabled=False),
        )
        ingestion = build_user_ingestion_from_deps(
            deps, user_id="u1", project_id="p1", scenario_id="s1"
        )
        assert ingestion.registry.prefix == f"{settings.registry_prefix}:user:u1:s1"
