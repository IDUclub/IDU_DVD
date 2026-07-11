"""Redis: parsing-job status store and document/version registry (replaces in-memory state)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

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

    def active(self) -> list[dict]:
        """Return queued and processing jobs, newest first."""
        jobs: list[dict] = []
        for key in self.r.scan_iter(match="dvd:job:*"):
            value = self.r.get(key)
            if not value:
                continue
            try:
                job = json.loads(value)
            except json.JSONDecodeError:
                continue
            if job.get("status") in {"queued", "processing"}:
                jobs.append(job)
        return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)


class DocumentRegistry:
    """Registry of uploaded documents: hashes (for deduplication) and versions per document name.

    All keys are namespaced by ``prefix`` (default ``dvd``; scoped to the physical collection
    via ``Settings.registry_prefix`` when collection namespacing is on), so dedup/version state
    tracks exactly what lives in the matching Qdrant collection. Keys (``P`` = prefix):

      P:hash:{content_hash} -> JSON {name, version, doc_id}   (presence = exact duplicate)
      P:versions:{name}     -> SET of versions of this document
      P:names               -> SET of all document names/designations (for reference matching)
      P:pending_ref:{key}   -> LIST of dangling references waiting for document `key` to arrive
      P:doc:{doc_id}        -> JSON document summary (for the document-level read API)
      P:docs                -> SET of all doc_ids
      P:blocks:{name}:{version} -> JSON list of source-block hashes (delta-update diffing)
    """

    def __init__(self, client: RedisClient, prefix: str = "dvd") -> None:
        self.r = client.r
        self.prefix = prefix

    def __repr__(self) -> str:
        return f"{type(self).__name__}(prefix={self.prefix})"

    def has_hash(self, content_hash: str) -> bool:
        return self.r.exists(f"{self.prefix}:hash:{content_hash}") > 0

    def hash_info(self, content_hash: str) -> dict | None:
        v = self.r.get(f"{self.prefix}:hash:{content_hash}")
        return json.loads(v) if v else None

    def versions(self, name: str) -> list[str]:
        return sorted(self.r.smembers(f"{self.prefix}:versions:{name}"))

    def version_exists(self, name: str, version: str) -> bool:
        return self.r.sismember(f"{self.prefix}:versions:{name}", version)

    def register(self, content_hash: str, name: str, version: str, doc_id: str) -> None:
        self.r.set(
            f"{self.prefix}:hash:{content_hash}",
            json.dumps(
                {"name": name, "version": version, "doc_id": doc_id}, ensure_ascii=False
            ),
        )
        self.r.sadd(f"{self.prefix}:versions:{name}", version)
        self.r.sadd(f"{self.prefix}:names", name)

    def remove_version(self, name: str, version: str) -> None:
        self.r.srem(f"{self.prefix}:versions:{name}", version)
        self.r.delete(f"{self.prefix}:blocks:{name}:{version}")

    def unregister_name(self, name: str) -> None:
        """Forget a document entirely: its version set, block fingerprints and name entry."""
        self.r.delete(f"{self.prefix}:versions:{name}")
        for key in self.r.scan_iter(match=f"{self.prefix}:blocks:{name}:*"):
            self.r.delete(key)
        self.r.srem(f"{self.prefix}:names", name)

    # --- source-block fingerprints (deterministic delta-update diffing) ---
    def register_blocks(self, name: str, version: str, hashes: list[str]) -> None:
        self.r.set(f"{self.prefix}:blocks:{name}:{version}", json.dumps(hashes))

    def get_blocks(self, name: str, version: str) -> list[str] | None:
        v = self.r.get(f"{self.prefix}:blocks:{name}:{version}")
        return json.loads(v) if v else None

    def remove_hashes(self, name: str, version: str | None = None) -> int:
        """Drop dedup-hash entries of a document (optionally of one version only)."""
        removed = 0
        for key in self.r.scan_iter(match=f"{self.prefix}:hash:*"):
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
        return sorted(self.r.smembers(f"{self.prefix}:names"))

    def has_name(self, name: str) -> bool:
        return self.r.sismember(f"{self.prefix}:names", name)

    # --- pending references (dangling links to not-yet-loaded documents) ---
    def _pending_key(self, norm_name: str) -> str:
        return f"{self.prefix}:pending_ref:{norm_name}"

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
        self.r.set(
            f"{self.prefix}:doc:{doc_id}", json.dumps(summary, ensure_ascii=False)
        )
        self.r.sadd(f"{self.prefix}:docs", doc_id)

    def get_document(self, doc_id: str) -> dict | None:
        v = self.r.get(f"{self.prefix}:doc:{doc_id}")
        return json.loads(v) if v else None

    def unregister_document(self, doc_id: str) -> None:
        self.r.delete(f"{self.prefix}:doc:{doc_id}")
        self.r.srem(f"{self.prefix}:docs", doc_id)

    def doc_ids(self) -> list[str]:
        return sorted(self.r.smembers(f"{self.prefix}:docs"))

    def all_documents(self) -> list[dict]:
        out = [self.get_document(d) for d in self.doc_ids()]
        return [d for d in out if d]

    def wipe(self) -> None:
        """Delete every key under this registry's prefix (whole-index teardown)."""
        for key in self.r.scan_iter(match=f"{self.prefix}:*"):
            self.r.delete(key)


class UserIndexRegistry:
    """Metadata registry of per-``(user_id, scenario_id)`` user-document indices.

    Separate from :class:`DocumentRegistry` (which tracks document content/versions inside one
    index): this tracks which indices exist and their inheritance link. Nested under
    ``Settings.registry_prefix`` — like ``DocumentRegistry`` — so it moves in lockstep with the
    physical collection when the embedding model changes. Keys (``P`` = prefix):

      P:user_index:{user_id}:{scenario_id} -> JSON {user_id, scenario_id, project_id,
                                                      parent_scenario_id, created_at}
      P:user_index:by_user:{user_id}       -> SET of scenario_ids belonging to that user
    """

    _MAX_CHAIN_DEPTH = 32

    def __init__(self, client: RedisClient, prefix: str) -> None:
        self.r = client.r
        self.prefix = prefix

    def __repr__(self) -> str:
        return f"{type(self).__name__}(prefix={self.prefix})"

    def _key(self, user_id: str, scenario_id: str) -> str:
        return f"{self.prefix}:user_index:{user_id}:{scenario_id}"

    def _by_user_key(self, user_id: str) -> str:
        return f"{self.prefix}:user_index:by_user:{user_id}"

    def get(self, user_id: str, scenario_id: str) -> dict | None:
        v = self.r.get(self._key(user_id, scenario_id))
        return json.loads(v) if v else None

    def _would_cycle(self, user_id: str, scenario_id: str, parent_scenario_id: str) -> bool:
        """True if ``parent_scenario_id`` already (transitively) descends from ``scenario_id``."""
        if parent_scenario_id == scenario_id:
            return True
        return scenario_id in self.ancestor_chain(user_id, parent_scenario_id)

    def create(
        self,
        user_id: str,
        scenario_id: str,
        project_id: str,
        parent_scenario_id: str | None = None,
    ) -> dict:
        if self.get(user_id, scenario_id) is not None:
            raise ValueError(f"index already exists: {user_id}/{scenario_id}")
        if parent_scenario_id and self._would_cycle(
            user_id, scenario_id, parent_scenario_id
        ):
            raise ValueError(
                f"parent_scenario_id would create an inheritance cycle: {parent_scenario_id}"
            )
        record = {
            "user_id": user_id,
            "scenario_id": scenario_id,
            "project_id": project_id,
            "parent_scenario_id": parent_scenario_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.r.set(self._key(user_id, scenario_id), json.dumps(record, ensure_ascii=False))
        self.r.sadd(self._by_user_key(user_id), scenario_id)
        return record

    def get_or_create(
        self,
        user_id: str,
        scenario_id: str,
        project_id: str,
        parent_scenario_id: str | None = None,
    ) -> dict:
        existing = self.get(user_id, scenario_id)
        if existing is not None:
            return existing
        return self.create(user_id, scenario_id, project_id, parent_scenario_id)

    def delete(self, user_id: str, scenario_id: str) -> None:
        self.r.delete(self._key(user_id, scenario_id))
        self.r.srem(self._by_user_key(user_id), scenario_id)

    def list_for_user(self, user_id: str) -> list[dict]:
        scenario_ids = self.r.smembers(self._by_user_key(user_id))
        out = [self.get(user_id, sid) for sid in scenario_ids]
        return sorted((rec for rec in out if rec), key=lambda r: r["scenario_id"])

    def ancestor_chain(self, user_id: str, scenario_id: str) -> list[str]:
        """``[scenario_id, parent, grandparent, ...]``.

        Cycle-safe (stops on a repeated id) and depth-capped, so a missing/deleted parent or an
        accidental cycle degrades to "just this scenario" instead of erroring.
        """
        chain = [scenario_id]
        seen = {scenario_id}
        current = scenario_id
        for _ in range(self._MAX_CHAIN_DEPTH):
            record = self.get(user_id, current)
            parent = record.get("parent_scenario_id") if record else None
            if not parent or parent in seen:
                break
            chain.append(parent)
            seen.add(parent)
            current = parent
        return chain
