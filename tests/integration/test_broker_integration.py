"""Integration: Kafka publishing (otteroad) against a real broker + Schema Registry.

End-to-end through the durable outbox: a ``DocumentProcessed`` event is enqueued to real
Redis, ``KafkaPublisher`` drains it into the ``document.events`` topic (registering the
AVRO schema in the Schema Registry on first send), and a throwaway consumer reads the
topic back and deserializes the event.

Needs ``DVD_KAFKA_BOOTSTRAP_SERVERS`` (and ``DVD_KAFKA_SCHEMA_REGISTRY_URL``) in the
environment — e.g. the IDU test cluster (external listeners, PLAINTEXT, no credentials):

    DVD_KAFKA_BOOTSTRAP_SERVERS=next.idulab.ru:9192,next.idulab.ru:9193,next.idulab.ru:9194
    DVD_KAFKA_SCHEMA_REGISTRY_URL=https://schema-registry.next.idulab.ru

Skips cleanly when the variable is unset or the broker is unreachable, like the rest of
the integration suite. Cleanup: the unique outbox/dead-letter Redis keys are deleted;
the produced event stays in the topic (events are append-only), marked by an
``itest-<uuid>`` document name.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from src.broker.events import DocumentProcessed
from src.broker.outbox import EventOutbox
from src.broker.publisher import KafkaPublisher

pytestmark = pytest.mark.integration

RECEIVE_DEADLINE = 60.0  # seconds to drain the outbox / find the event in the topic


@pytest.fixture
def require_kafka(live_settings):
    """Skip unless a broker is configured and answers metadata queries."""
    if not live_settings.kafka_bootstrap_servers:
        pytest.skip("DVD_KAFKA_BOOTSTRAP_SERVERS is not set")
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": live_settings.kafka_bootstrap_servers})
    try:
        # Generous timeout: first metadata from a remote cluster can take >10s.
        admin.list_topics(timeout=30)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Kafka unavailable: {exc}")


@pytest.fixture
def broker_settings(live_settings, require_kafka):
    """Per-test Redis keys so parallel/aborted runs never share outbox state."""
    run_id = uuid.uuid4().hex[:8]
    return live_settings.model_copy(
        update={
            "kafka_outbox_key": f"itest:kafka:outbox:{run_id}",
            "kafka_dead_letter_key": f"itest:kafka:outbox:dead:{run_id}",
        }
    )


async def test_outbox_event_reaches_kafka(broker_settings, require_redis):
    marker = f"itest-{uuid.uuid4().hex[:12]}"
    outbox = EventOutbox(require_redis, broker_settings)
    publisher = KafkaPublisher(outbox, broker_settings)

    try:
        await publisher.start()
        assert publisher.enabled

        outbox.enqueue(DocumentProcessed(document_name=marker))

        # The publisher removes the entry only after the broker confirms delivery.
        deadline = time.monotonic() + RECEIVE_DEADLINE
        while outbox.size() > 0:
            assert time.monotonic() < deadline, "outbox was not drained in time"
            assert require_redis.r.llen(outbox.dead_key) == 0, "event dead-lettered"
            await asyncio.sleep(0.5)

        # Read the topic back and deserialize with the producer's own serializer.
        assert _find_event(
            publisher, broker_settings, marker
        ), f"event {marker} not found in topic {DocumentProcessed.topic}"
    finally:
        await publisher.stop()
        require_redis.r.delete(outbox.key, outbox.dead_key)


def _find_event(publisher: KafkaPublisher, settings, marker: str) -> bool:
    """Scan ``document.events`` from the beginning for our marker event.

    Deserializes via ``DocumentProcessed.deserialize`` directly rather than
    ``publisher._producer.deserialize_message``: otteroad's generic model
    lookup (``AvroSerializerMixin._get_model_class``) resolves a schema ID to
    a model class by comparing the registry's schema string (stored with
    default ``json.dumps`` spacing) against a compact-separator re-dump of
    each candidate model's schema — the two never match, so it always warns
    "No registered model for given schema" and returns None. Since we already
    know which model we published, ask that model to deserialize the payload
    itself and skip the broken lookup.
    """
    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": f"itest-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    try:
        consumer.subscribe([DocumentProcessed.topic])
        deadline = time.monotonic() + RECEIVE_DEADLINE
        while time.monotonic() < deadline:
            msg = consumer.poll(timeout=1.0)
            if msg is None or msg.error():
                continue
            event = DocumentProcessed.deserialize(
                msg.value(), publisher._producer.schema_registry
            )
            if event.document_name == marker:
                return True
        return False
    finally:
        consumer.close()
