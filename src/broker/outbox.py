"""Redis-backed outbox for Kafka events (at-least-once delivery).

The ingestion pipeline runs in worker threads and must not depend on Kafka
availability, so instead of producing directly it appends events to a Redis
list. The async :class:`~src.broker.publisher.KafkaPublisher` drains that list
into Kafka, removing an entry only after the broker confirms delivery; entries
that keep failing are moved to a dead-letter list instead of blocking the queue.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from otteroad.avro import AvroEventModel

from src.common.config import Settings
from src.common.db.redis_client import RedisClient


class EventOutbox:
    """Pending Kafka events. Keys: {kafka_outbox_key} (queue) + dead-letter list."""

    def __init__(self, client: RedisClient, settings: Settings) -> None:
        self.r = client.r
        self.key = settings.kafka_outbox_key
        self.dead_key = settings.kafka_dead_letter_key

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key={self.key})"

    def enqueue(self, event: AvroEventModel) -> None:
        """Append an event to the queue (called from the ingestion pipeline)."""
        entry = {
            "model": type(event).__name__,
            "payload": event.model_dump(mode="json"),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
        }
        self.r.rpush(self.key, json.dumps(entry, ensure_ascii=False))

    def peek(self) -> dict | None:
        """Head of the queue without removing it (publisher removes it only after delivery)."""
        v = self.r.lindex(self.key, 0)
        return json.loads(v) if v else None

    def commit(self) -> None:
        """Drop the head entry after a confirmed delivery."""
        self.r.lpop(self.key)

    def record_failure(self, entry: dict, max_attempts: int) -> bool:
        """Count a failed send for the head entry.

        Returns True if the entry was dead-lettered (attempt limit reached),
        False if it stays at the head for another retry.
        """
        entry = {**entry, "attempts": int(entry.get("attempts", 0)) + 1}
        if entry["attempts"] >= max_attempts:
            pipe = self.r.pipeline()
            pipe.lpop(self.key)
            pipe.rpush(self.dead_key, json.dumps(entry, ensure_ascii=False))
            pipe.execute()
            return True
        self.r.lset(self.key, 0, json.dumps(entry, ensure_ascii=False))
        return False

    def size(self) -> int:
        return self.r.llen(self.key)


class ScopedEventOutbox:
    """Stamps ``user_id``/``scenario_id`` onto every event before forwarding to a real outbox.

    Lets the per-request user-scoped ``IngestionService`` announce lifecycle events without any
    change to its own ``self.outbox.enqueue(...)`` call sites.
    """

    def __init__(self, inner: EventOutbox, *, user_id: str, scenario_id: str) -> None:
        self._inner = inner
        self._user_id = user_id
        self._scenario_id = scenario_id

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(inner={self._inner!r}, "
            f"user_id={self._user_id}, scenario_id={self._scenario_id})"
        )

    def enqueue(self, event: AvroEventModel) -> None:
        event = event.model_copy(
            update={"user_id": self._user_id, "scenario_id": self._scenario_id}
        )
        self._inner.enqueue(event)
