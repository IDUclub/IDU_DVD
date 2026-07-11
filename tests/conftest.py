"""Shared test fixtures and lightweight fakes.

Point the global configuration at the local stack (Docker Compose services + local Ollama)
*before* any ``src`` module is imported, so the module-level ``settings`` singleton — and the
``OllamaClient`` default base that reads it — resolve to localhost. Real environment variables
always win (``setdefault``), so CI or a custom ``.env`` can override these.
"""

from __future__ import annotations

import os

os.environ.setdefault("DVD_OLLAMA_BASE", "http://localhost:11434")
os.environ.setdefault("DVD_OLLAMA_MODEL", "qwen2.5:7b-instruct")
os.environ.setdefault("DVD_OLLAMA_EMBED_MODEL", "bge-m3")
os.environ.setdefault("DVD_QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("DVD_REDIS_URL", "redis://localhost:6379/0")

import re
from types import SimpleNamespace

import pytest

from src.common.config import Settings


# --------------------------------------------------------------------------------------
# Fakes — in-memory doubles so unit tests never touch Qdrant / Redis / Ollama / the network
# --------------------------------------------------------------------------------------
def parse_window_ids(user: str) -> list[int]:
    """Extract the ``[i]`` indices the LLM prompt was built from (see modules' ``_llm_*``)."""
    return [int(m) for m in re.findall(r"^\[(\d+)\]", user, re.M)]


def pipeline_chat_handler(system: str, user: str, schema: dict) -> dict:
    """Deterministic LLM stand-in covering every JSON schema used across the pipeline.

    Routes purely by the schema shape, so the same handler answers Stage-1 boundaries,
    Stage-1.5 semantic merge, Stage-2 structure, tagging, and version detection.
    """
    props = schema.get("properties", {})
    ids = parse_window_ids(user)
    if "blocks" in props:  # Stage 1: boundaries
        return {"blocks": [{"id": i, "boundary": "new"} for i in ids]}
    if "parts" in props:  # Stage 1.5: semantic merge
        return {"parts": [{"id": i, "merge_with_previous": False} for i in ids]}
    if "nodes" in props:  # Stage 2: structure — now also carries fragment tags
        item_props = props["nodes"]["items"]["properties"]
        out = []
        for i in ids:
            node = {"id": i}
            if "type" in item_props:
                node.update(
                    {
                        "type": "paragraph",
                        "numbering": "",
                        "relation": "deeper",
                        "block": "main",
                    }
                )
            if "tags" in item_props:
                node["tags"] = [f"tag{i}"]
            out.append(node)
        return {"nodes": out}
    if "items" in props:  # reference extraction — no references by default
        return {"items": [{"id": i, "references": []} for i in ids]}
    if "name" in props and "version" in props:  # version detection
        return {"name": "ТЕСТ 1", "version": "ТЕСТ 1 ред. 1"}
    raise AssertionError(f"unexpected schema: {schema}")


class FakeOllama:
    """Stand-in for ``OllamaClient``: programmable ``chat`` and deterministic ``embed``.

    Records every call so tests can assert how the modules drove the LLM.
    """

    def __init__(self, chat_handler=pipeline_chat_handler, embed_dim: int = 8) -> None:
        self._chat_handler = chat_handler
        self._embed_dim = embed_dim
        self.chat_calls: list[tuple] = []
        self.embed_calls: list[list[str]] = []
        self.closed = False

    def chat(
        self, system: str, user: str, schema: dict, model: str | None = None
    ) -> dict:
        self.chat_calls.append((system, user, schema))
        return self._chat_handler(system, user, schema)

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [
            [float((len(t) + i) % 7) for i in range(self._embed_dim)] for t in texts
        ]

    # Provider-agnostic embedder surface (mirrors OllamaClient / GigaEmbeddingsClient).
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def available(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeOllama":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeQdrantRepo:
    """In-memory double of ``QdrantRepository`` with the same surface the services use."""

    def __init__(self) -> None:
        self.points: dict[str, tuple] = {}  # id -> (vector, payload)
        self.set_other_versions_calls: list[tuple] = []
        self.ensured = False
        self.collection = "documents"  # ScopedQdrantRepository passthrough target
        self.settings = None

    def ensure_collection(self) -> None:
        self.ensured = True

    def upsert(self, points) -> int:
        points = list(points)
        for p in points:
            self.points[str(p.id)] = (p.vector, p.payload)
        return len(points)

    def search(self, vector, query_filter, limit):
        out = []
        for i, (pid, (_vec, payload)) in enumerate(self.points.items()):
            if query_filter is not None and not self._matches(payload, query_filter):
                continue
            out.append(SimpleNamespace(id=pid, score=1.0 - 0.01 * i, payload=payload))
            if len(out) >= limit:
                break
        return out

    def retrieve(self, ids):
        return {str(i): self.points[str(i)][1] for i in ids if str(i) in self.points}

    def get_point(self, point_id):
        return self.points.get(str(point_id))

    def replace_point(self, point_id, vector, payload):
        self.points[str(point_id)] = (list(vector), dict(payload))

    def set_point_payload(self, point_id, payload):
        vector, current = self.points[str(point_id)]
        self.points[str(point_id)] = (vector, {**current, **payload})

    def set_document_payload(self, doc_id, payload):
        for point_id, (vector, current) in list(self.points.items()):
            if current.get("doc_id") == doc_id:
                self.points[point_id] = (vector, {**current, **payload})

    def set_other_versions(
        self, name, version, other_versions, extra_must=None
    ) -> None:
        self.set_other_versions_calls.append((name, version, other_versions))

    def find_node(self, name, numbering="", version=None, extra_must=None):
        best = None
        for pid, (_vec, pl) in self.points.items():
            if pl.get("name") != name:
                continue
            if numbering and pl.get("numbering") != numbering:
                continue
            if version and pl.get("version") != version:
                continue
            if extra_must and not all(self._matches(pl, c) for c in extra_must):
                continue
            cand = {
                "node_id": pid,
                "doc_id": pl.get("doc_id"),
                "version": pl.get("version"),
                "numbering": pl.get("numbering", ""),
            }
            if best is None or (pl.get("version", "") > best[1]):
                best = (cand, pl.get("version", ""))
        return best[0] if best else None

    @staticmethod
    def _matches(pl, cond) -> bool:
        if hasattr(cond, "must"):  # a nested Filter: AND(must) & OR(should)
            must = cond.must or []
            should = cond.should or []
            if not all(FakeQdrantRepo._matches(pl, c) for c in must):
                return False
            if should and not any(FakeQdrantRepo._matches(pl, c) for c in should):
                return False
            return True
        is_empty = getattr(cond, "is_empty", None)
        if is_empty is not None:
            val = pl.get(is_empty.key)
            return val is None or val == "" or val == []
        val = pl.get(cond.key)
        m = cond.match
        if hasattr(m, "value"):
            if isinstance(val, list):
                return m.value in val
            return val == m.value
        if hasattr(m, "any"):
            if isinstance(val, list):
                return any(v in val for v in m.any)
            return val in m.any
        return False

    def scroll_payloads(self, query_filter=None, batch=256):
        payloads = [pl for _vec, pl in self.points.values()]
        if query_filter is None:
            return payloads
        return [pl for pl in payloads if self._matches(pl, query_filter)]

    def count(self, query_filter=None) -> int:
        return len(self.scroll_payloads(query_filter))

    def points_by_name(self, name, extra_must=None):
        return [
            {**pl, "id": str(pid)}
            for pid, (_vec, pl) in self.points.items()
            if pl.get("name") == name
            and (not extra_must or all(self._matches(pl, c) for c in extra_must))
        ]

    def set_versions(self, point_ids, versions) -> None:
        for pid in point_ids:
            vec, pl = self.points[str(pid)]
            self.points[str(pid)] = (vec, {**pl, "versions": list(versions)})

    def delete_points(self, point_ids) -> None:
        for pid in point_ids:
            self.points.pop(str(pid), None)

    def delete_by_name(self, name, extra_must=None) -> None:
        self.points = {
            pid: (vec, pl)
            for pid, (vec, pl) in self.points.items()
            if pl.get("name") != name
            or (extra_must and not all(self._matches(pl, c) for c in extra_must))
        }

    def delete_by_filter(self, query_filter) -> None:
        self.points = {
            pid: (vec, pl)
            for pid, (vec, pl) in self.points.items()
            if not self._matches(pl, query_filter)
        }

    def update_references(self, node_id, references) -> None:
        nid = str(node_id)
        if nid in self.points:
            vec, pl = self.points[nid]
            pl = dict(pl)
            pl["references"] = references
            self.points[nid] = (vec, pl)

    def list_by_doc(self, doc_id, limit=10000, extra_must=None):
        out = []
        for pid, (_vec, payload) in self.points.items():
            if (payload or {}).get("doc_id") != doc_id:
                continue
            if extra_must and not all(self._matches(payload, c) for c in extra_must):
                continue
            out.append({**payload, "id": str(pid)})
        return out

    def doc_ids_by_lookup_key(self, key, limit=1000):
        seen = []
        for _pid, (_vec, payload) in self.points.items():
            if key in (payload or {}).get("lookup_keys", []):
                did = (payload or {}).get("doc_id")
                if did and did not in seen:
                    seen.append(did)
        return seen


class FakeDocumentStorage:
    """In-memory double of ``DocumentStorage`` — no real MinIO/network involved."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}  # key -> (data, content_type)
        self.upload_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fail_upload = False

    def ensure_bucket(self) -> None:
        pass

    def upload(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        if self.fail_upload:
            raise RuntimeError("simulated MinIO failure")
        self.upload_calls.append(key)
        self.objects[key] = (data, content_type)

    def download(self, key: str) -> tuple[bytes, str | None]:
        if key not in self.objects:
            from minio.error import S3Error

            raise S3Error(None, "NoSuchKey", "not found", key, "req", "host")
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        self.objects.pop(key, None)


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------
@pytest.fixture
def settings() -> Settings:
    """Fresh ``Settings`` with project defaults (no live services touched)."""
    return Settings()


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def fake_qdrant() -> FakeQdrantRepo:
    return FakeQdrantRepo()


@pytest.fixture
def fake_document_storage() -> FakeDocumentStorage:
    return FakeDocumentStorage()


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch ``redis.Redis.from_url`` so the real store classes run against fakeredis."""
    import fakeredis
    import redis

    server = fakeredis.FakeServer()

    def _from_url(url, **kwargs):
        return fakeredis.FakeRedis(server=server, **kwargs)

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(_from_url))
    return server


@pytest.fixture
def user_index_registry(settings, fake_redis):
    """A real ``UserIndexRegistry`` backed by fakeredis (needs the ``fake_redis`` patch active)."""
    from src.common.db.redis_client import RedisClient, UserIndexRegistry

    return UserIndexRegistry(RedisClient(settings), prefix="test")


@pytest.fixture
def sample_raw() -> list[dict]:
    """Raw blocks as produced by ``DocumentParser.extract_raw`` (post-unstructured)."""
    return [
        {
            "text": "СП 99.99999.2099 Тестовый свод правил",
            "category": "Title",
            "html": None,
        },
        {"text": "1 Область применения", "category": "NarrativeText", "html": None},
        {
            "text": "Настоящий документ устанавливает требования.",
            "category": "NarrativeText",
            "html": None,
        },
        {"text": "2 Нормативные ссылки", "category": "NarrativeText", "html": None},
    ]


@pytest.fixture
def sample_parts() -> list[dict]:
    """Logical parts after structure tagging — input to ``HierarchyBuilder.build``."""
    return [
        {
            "id": 0,
            "text": "Раздел 1",
            "numbering": "1",
            "type": "chapter",
            "relation": "top",
            "block": "main",
            "category": "Title",
            "html": None,
        },
        {
            "id": 1,
            "text": "Пункт 1.1",
            "numbering": "1.1",
            "type": "clause",
            "relation": "deeper",
            "block": "main",
            "category": "NarrativeText",
            "html": None,
        },
        {
            "id": 2,
            "text": "Пункт 1.2",
            "numbering": "1.2",
            "type": "clause",
            "relation": "same",
            "block": "main",
            "category": "NarrativeText",
            "html": None,
        },
    ]


@pytest.fixture
def sample_rank_map() -> dict[str, int]:
    return {"1": 1, "1.1": 2, "1.2": 2}
