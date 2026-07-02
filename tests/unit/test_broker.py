"""Unit tests for src/broker — Kafka event model, Redis outbox and publisher drain loop.

Hermetic: Redis is fakeredis, the otteroad producer is a mock — no broker or Schema
Registry is touched. Covers: outbox FIFO semantics (enqueue/peek/commit), retry
accounting with dead-lettering, the publisher's idle/sent/failed outcomes, the
unknown-event guard, and the disabled-mode no-op.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.broker.events import DocumentProcessed
from src.broker.outbox import EventOutbox
from src.broker.publisher import KafkaPublisher
from src.common.config import Settings
from src.common.db.redis_client import RedisClient


@pytest.fixture
def outbox(settings, fake_redis) -> EventOutbox:
    return EventOutbox(RedisClient(settings), settings)


def _kafka_settings() -> Settings:
    return Settings(kafka_bootstrap_servers="kafka:9092")


class TestDocumentProcessedModel:
    def test_topic_and_schema_metadata(self):
        assert DocumentProcessed.topic == "document.events"
        assert DocumentProcessed.namespace == "documents"
        assert DocumentProcessed.schema_version == 1

    def test_payload_roundtrip(self):
        e = DocumentProcessed(document_name="СП 99.99999.2099")
        assert DocumentProcessed(**e.model_dump(mode="json")) == e


class TestEventOutbox:
    def test_enqueue_peek_commit_fifo(self, outbox):
        outbox.enqueue(DocumentProcessed(document_name="doc A"))
        outbox.enqueue(DocumentProcessed(document_name="doc B"))
        assert outbox.size() == 2

        head = outbox.peek()
        assert head["model"] == "DocumentProcessed"
        assert head["payload"] == {"document_name": "doc A"}
        assert head["attempts"] == 0
        assert head["enqueued_at"]

        outbox.commit()
        assert outbox.peek()["payload"] == {"document_name": "doc B"}
        outbox.commit()
        assert outbox.peek() is None
        assert outbox.size() == 0

    def test_peek_does_not_remove(self, outbox):
        outbox.enqueue(DocumentProcessed(document_name="doc"))
        assert outbox.peek() == outbox.peek()
        assert outbox.size() == 1

    def test_record_failure_increments_attempts_in_place(self, outbox):
        outbox.enqueue(DocumentProcessed(document_name="doc"))
        dead = outbox.record_failure(outbox.peek(), max_attempts=3)
        assert dead is False
        assert outbox.peek()["attempts"] == 1
        assert outbox.size() == 1

    def test_record_failure_dead_letters_at_limit(self, outbox):
        outbox.enqueue(DocumentProcessed(document_name="doc"))
        entry = outbox.peek()
        assert outbox.record_failure(entry, max_attempts=2) is False
        assert outbox.record_failure(outbox.peek(), max_attempts=2) is True

        assert outbox.size() == 0
        dead = [json.loads(v) for v in outbox.r.lrange(outbox.dead_key, 0, -1)]
        assert len(dead) == 1
        assert dead[0]["attempts"] == 2
        assert dead[0]["payload"] == {"document_name": "doc"}


class TestKafkaPublisher:
    def test_disabled_without_bootstrap_servers(self, outbox, settings):
        publisher = KafkaPublisher(outbox, settings)
        assert publisher.enabled is False

    async def test_start_is_noop_when_disabled(self, outbox, settings):
        publisher = KafkaPublisher(outbox, settings)
        await publisher.start()
        assert publisher._producer is None
        assert publisher._task is None
        await publisher.stop()

    async def test_drain_idle_on_empty_outbox(self, outbox):
        publisher = KafkaPublisher(outbox, _kafka_settings())
        publisher._producer = AsyncMock()
        assert await publisher._drain_once() == "idle"
        publisher._producer.send.assert_not_awaited()

    async def test_drain_sends_and_commits(self, outbox):
        publisher = KafkaPublisher(outbox, _kafka_settings())
        publisher._producer = AsyncMock()
        outbox.enqueue(DocumentProcessed(document_name="doc"))

        assert await publisher._drain_once() == "sent"

        (event,) = publisher._producer.send.await_args.args
        assert event == DocumentProcessed(document_name="doc")
        assert outbox.size() == 0

    async def test_drain_failure_keeps_entry_and_counts_attempt(self, outbox):
        publisher = KafkaPublisher(outbox, _kafka_settings())
        publisher._producer = AsyncMock()
        publisher._producer.send.side_effect = RuntimeError("broker down")
        outbox.enqueue(DocumentProcessed(document_name="doc"))

        assert await publisher._drain_once() == "failed"
        assert outbox.size() == 1
        assert outbox.peek()["attempts"] == 1

    async def test_drain_dead_letters_after_max_attempts(self, outbox):
        s = _kafka_settings()
        publisher = KafkaPublisher(outbox, s)
        publisher._producer = AsyncMock()
        publisher._producer.send.side_effect = RuntimeError("broker down")
        outbox.enqueue(DocumentProcessed(document_name="doc"))

        for _ in range(s.kafka_max_attempts):
            assert await publisher._drain_once() == "failed"

        assert outbox.size() == 0
        assert outbox.r.llen(outbox.dead_key) == 1

    async def test_unknown_event_model_is_dead_lettered(self, outbox):
        publisher = KafkaPublisher(outbox, _kafka_settings())
        publisher._producer = AsyncMock()
        outbox.r.rpush(
            outbox.key, json.dumps({"model": "NoSuchEvent", "payload": {}})
        )

        assert await publisher._drain_once() == "sent"
        publisher._producer.send.assert_not_awaited()
        assert outbox.size() == 0
        assert outbox.r.llen(outbox.dead_key) == 1
