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
      dvd:names               -> SET of all document names/designations (for reference matching)
      dvd:pending_ref:{key}   -> LIST of dangling references waiting for document `key` to arrive
      dvd:doc:{doc_id}        -> JSON document summary (for the document-level read API)
      dvd:docs                -> SET of all doc_ids
      dvd:blocks:{name}:{version} -> JSON list of source-block hashes (delta-update diffing)
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
        self.r.sadd("dvd:names", name)

    def remove_version(self, name: str, version: str) -> None:
        self.r.srem(f"dvd:versions:{name}", version)
        self.r.delete(f"dvd:blocks:{name}:{version}")

    def unregister_name(self, name: str) -> None:
        """Forget a document entirely: its version set, block fingerprints and name entry."""
        self.r.delete(f"dvd:versions:{name}")
        for key in self.r.scan_iter(match=f"dvd:blocks:{name}:*"):
            self.r.delete(key)
        self.r.srem("dvd:names", name)

    # --- source-block fingerprints (deterministic delta-update diffing) ---
    def register_blocks(self, name: str, version: str, hashes: list[str]) -> None:
        self.r.set(f"dvd:blocks:{name}:{version}", json.dumps(hashes))

    def get_blocks(self, name: str, version: str) -> list[str] | None:
        v = self.r.get(f"dvd:blocks:{name}:{version}")
        return json.loads(v) if v else None

    def remove_hashes(self, name: str, version: str | None = None) -> int:
        """Drop dedup-hash entries of a document (optionally of one version only)."""
        removed = 0
        for key in self.r.scan_iter(match="dvd:hash:*"):
            v = self.r.get(key)
            info = json.loads(v) if v else {}
            if info.get("name") != name:
                continue
            if version is not None and info.get("version") != version:
                continue
            self.r.delete(key)
            removed += 1
        return removed

    # --- document names (for reference resolution) ---
    def names(self) -> list[str]:
        """All document names/designations ever registered (for reference matching)."""
        return sorted(self.r.smembers("dvd:names"))

    def has_name(self, name: str) -> bool:
        return self.r.sismember("dvd:names", name)

    # --- pending references (dangling links to not-yet-loaded documents) ---
    @staticmethod
    def _pending_key(norm_name: str) -> str:
        return f"dvd:pending_ref:{norm_name}"

    def add_pending(self, norm_name: str, entry: dict) -> None:
        """Record a reference whose target document is not loaded yet, keyed by its normalized name."""
        self.r.rpush(
            self._pending_key(norm_name), json.dumps(entry, ensure_ascii=False)
        )

    def peek_pending(self, norm_name: str) -> list[dict]:
        """Read the dangling references for a normalized name without removing them."""
        return [
            json.loads(v) for v in self.r.lrange(self._pending_key(norm_name), 0, -1)
        ]

    def pop_pending(self, norm_name: str) -> list[dict]:
        """Read and atomically clear the dangling references for a normalized name."""
        key = self._pending_key(norm_name)
        pipe = self.r.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        vals, _ = pipe.execute()
        return [json.loads(v) for v in vals]

    # --- document summaries (for the document-level read API) ---
    def register_document(self, doc_id: str, summary: dict) -> None:
        self.r.set(f"dvd:doc:{doc_id}", json.dumps(summary, ensure_ascii=False))
        self.r.sadd("dvd:docs", doc_id)

    def get_document(self, doc_id: str) -> dict | None:
        v = self.r.get(f"dvd:doc:{doc_id}")
        return json.loads(v) if v else None

    def unregister_document(self, doc_id: str) -> None:
        self.r.delete(f"dvd:doc:{doc_id}")
        self.r.srem("dvd:docs", doc_id)

    def doc_ids(self) -> list[str]:
        return sorted(self.r.smembers("dvd:docs"))

    def all_documents(self) -> list[dict]:
        out = [self.get_document(d) for d in self.doc_ids()]
        return [d for d in out if d]
