"""Background publisher: drains the Redis outbox into Kafka via otteroad.

Runs as an asyncio task inside the FastAPI lifespan. Disabled entirely when
``DVD_KAFKA_BOOTSTRAP_SERVERS`` is not set, so local setups without a broker
work unchanged.
"""

from __future__ import annotations

import asyncio

import structlog
from otteroad import KafkaProducerClient, KafkaProducerSettings
from otteroad.avro import AvroEventModel

from src.broker.events import DocumentProcessed
from src.broker.outbox import EventOutbox
from src.common.config import Settings

log = structlog.get_logger(__name__)

# Registered event types the outbox may contain, by class name (entries store
# the class name + payload, not pickled objects).
EVENT_MODELS: dict[str, type[AvroEventModel]] = {
    DocumentProcessed.__name__: DocumentProcessed,
}


class KafkaPublisher:
    """Owns the otteroad producer and the outbox drain loop."""

    def __init__(self, outbox: EventOutbox, settings: Settings) -> None:
        self.outbox = outbox
        self.settings = settings
        self._producer: KafkaProducerClient | None = None
        self._task: asyncio.Task | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(enabled={self.enabled}, "
            f"servers={self.settings.kafka_bootstrap_servers})"
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.kafka_bootstrap_servers)

    async def start(self) -> None:
        """Create the producer and start draining (no-op when Kafka is not configured)."""
        if not self.enabled:
            log.info("kafka_publisher_disabled")
            return
        producer_settings = KafkaProducerSettings(
            bootstrap_servers=self.settings.kafka_bootstrap_servers,
            client_id=self.settings.kafka_client_id,
            schema_registry_url=self.settings.kafka_schema_registry_url,
        )
        self._producer = KafkaProducerClient(producer_settings, logger=log)
        await self._producer.start()
        self._task = asyncio.create_task(self._drain_loop(), name="kafka-outbox-drain")
        log.info(
            "kafka_publisher_started",
            servers=self.settings.kafka_bootstrap_servers,
            outbox=self.outbox.key,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._producer:
            await self._producer.close()
            self._producer = None
        log.info("kafka_publisher_stopped")

    async def _drain_loop(self) -> None:
        while True:
            try:
                outcome = await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep draining no matter what
                log.error("kafka_drain_error", error=str(exc))
                outcome = "failed"
            if outcome == "idle":
                await asyncio.sleep(self.settings.kafka_poll_interval)
            elif outcome == "failed":
                await asyncio.sleep(self.settings.kafka_retry_interval)

    async def _drain_once(self) -> str:
        """Try to deliver the head outbox entry. Returns 'idle' | 'sent' | 'failed'."""
        entry = await asyncio.to_thread(self.outbox.peek)
        if entry is None:
            return "idle"

        model_cls = EVENT_MODELS.get(entry.get("model", ""))
        if model_cls is None:
            # Unknown event type can never be sent — dead-letter immediately.
            log.error("kafka_unknown_event_model", model=entry.get("model"))
            await asyncio.to_thread(self.outbox.record_failure, entry, 0)
            return "sent"

        try:
            event = model_cls(**entry.get("payload", {}))
            await self._producer.send(event)
        except Exception as exc:  # noqa: BLE001 — failure is recorded, not raised
            dead = await asyncio.to_thread(
                self.outbox.record_failure, entry, self.settings.kafka_max_attempts
            )
            log.warning(
                "kafka_send_failed",
                model=entry.get("model"),
                attempts=int(entry.get("attempts", 0)) + 1,
                dead_lettered=dead,
                error=str(exc),
            )
            return "failed"

        await asyncio.to_thread(self.outbox.commit)
        log.info("kafka_event_sent", model=entry.get("model"), topic=model_cls.topic)
        return "sent"
