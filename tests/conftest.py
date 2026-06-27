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
    if "nodes" in props:
        item_props = props["nodes"]["items"]["properties"]
        if "tags" in item_props:  # tagging
            return {"nodes": [{"id": i, "tags": [f"tag{i}"]} for i in ids]}
        return {
            "nodes": [
                {
                    "id": i,
                    "type": "paragraph",
                    "numbering": "",  # structure
                    "relation": "deeper",
                    "block": "main",
                }
                for i in ids
            ]
        }
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
            out.append(SimpleNamespace(id=pid, score=1.0 - 0.01 * i, payload=payload))
            if len(out) >= limit:
                break
        return out

    def retrieve(self, ids):
        return {str(i): self.points[str(i)][1] for i in ids if str(i) in self.points}

    def set_other_versions(self, name, version, other_versions) -> None:
        self.set_other_versions_calls.append((name, version, other_versions))

    def find_node(self, name, numbering="", version=None):
        best = None
        for pid, (_vec, pl) in self.points.items():
            if pl.get("name") != name:
                continue
            if numbering and pl.get("numbering") != numbering:
                continue
            if version and pl.get("version") != version:
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

    def update_references(self, node_id, references) -> None:
        nid = str(node_id)
        if nid in self.points:
            vec, pl = self.points[nid]
            pl = dict(pl)
            pl["references"] = references
            self.points[nid] = (vec, pl)


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
