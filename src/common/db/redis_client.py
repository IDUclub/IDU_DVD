"""Redis: parsing-job status store and document/version registry (replaces in-memory state)."""

from __future__ import annotations

import json

import redis
import structlog

from src.common.config import Settings

log = structlog.get_logger(__name__)


class RedisClient:
    """Thin wrapper around redis-py with decode_responses enabled."""

    def __init__(self, settings: Settings) -> None:
        self.url = settings.redis_url
        self.r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        self._ttl = settings.redis_job_ttl

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.url}, job_ttl={self._ttl})"

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_unavailable", error=str(exc))
            return False


class JobStore:
    """Background parsing-job status. Key: dvd:job:{job_id}."""

    def __init__(self, client: RedisClient) -> None:
        self.r = client.r
        self.ttl = client._ttl

    def __repr__(self) -> str:
        return f"{type(self).__name__}(ttl={self.ttl})"

    @staticmethod
    def _key(job_id: str) -> str:
        return f"dvd:job:{job_id}"

    def set(self, job_id: str, data: dict) -> None:
        self.r.set(self._key(job_id), json.dumps(data, ensure_ascii=False), ex=self.ttl)

    def get(self, job_id: str) -> dict | None:
        v = self.r.get(self._key(job_id))
        return json.loads(v) if v else None

    def update(self, job_id: str, **fields) -> None:
        data = self.get(job_id) or {"job_id": job_id}
        data.update(fields)
        self.set(job_id, data)


class DocumentRegistry:
    """Registry of uploaded documents: hashes (for deduplication) and versions per document name.

    Keys:
      dvd:hash:{content_hash} -> JSON {name, version, doc_id}   (presence = exact duplicate)
      dvd:versions:{name}     -> SET of versions of this document
    """

    def __init__(self, client: RedisClient) -> None:
        self.r = client.r

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def has_hash(self, content_hash: str) -> bool:
        return self.r.exists(f"dvd:hash:{content_hash}") > 0

    def hash_info(self, content_hash: str) -> dict | None:
        v = self.r.get(f"dvd:hash:{content_hash}")
        return json.loads(v) if v else None

    def versions(self, name: str) -> list[str]:
        return sorted(self.r.smembers(f"dvd:versions:{name}"))

    def version_exists(self, name: str, version: str) -> bool:
        return self.r.sismember(f"dvd:versions:{name}", version)

    def register(self, content_hash: str, name: str, version: str, doc_id: str) -> None:
        self.r.set(
            f"dvd:hash:{content_hash}",
            json.dumps(
                {"name": name, "version": version, "doc_id": doc_id}, ensure_ascii=False
            ),
        )
        self.r.sadd(f"dvd:versions:{name}", version)
