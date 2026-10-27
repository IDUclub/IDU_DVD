"""Hermetic request bodies and job-origin isolation for shared LLM scheduling."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, current_thread
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.api_clients.base import LlmError
from src.api_clients.llm_client import OpenAICompatibleClient
from src.api_clients.ocr_client import DotsOcrClient
from src.api_clients.request_priority import (
    DEFAULT_PRIORITIES,
    RequestPriority,
    _priorities,
)
from src.api_clients.vision_client import VisionClient
from src.common.config import Settings
from src.common.db.redis_client import RedisClient
from src.dvd_service.modules.structure import STRUCT_SCHEMA
from src.dvd_service.modules.windowing import chat_window, map_concurrent
from src.dvd_service.services.amendment_service import AmendmentService
from src.dvd_service.services.dvd_service import IngestionService
from src.dvd_service.services.user_index_service import build_user_ingestion_from_deps


@pytest.fixture
def priority_settings(settings):
    return settings.model_copy(
        update={
            "llm_provider": "openai",
            "llm_request_priority_enabled": True,
            "llm_request_priorities": "",
            "vision_base_url": "",
        }
    )


def answer(content="{}"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def text_client(settings, handler):
    client = OpenAICompatibleClient(priority_settings=settings)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_defaults_and_user_override(priority_settings):
    expected = {
        "ingest.structure": 100,
        "ingest.references": 100,
        "ingest.relations": 100,
        "ingest.version": 100,
        "ingest.amendments": 100,
        "ingest.vision": 100,
        "ingest.user": 20,
        "query": -100,
    }
    assert DEFAULT_PRIORITIES == expected
    corpus = RequestPriority(priority_settings)
    user = RequestPriority(priority_settings, user_document=True)
    for kind, value in expected.items():
        assert corpus.resolve(kind) == value
        assert user.resolve(kind) == (20 if kind.startswith("ingest.") else value)


def test_overrides_warn_once_and_ignore_invalid_entries(priority_settings, monkeypatch):
    warning = Mock()
    monkeypatch.setattr("src.api_clients.request_priority.log.warning", warning)
    _priorities.cache_clear()
    priority_settings.llm_request_priorities = (
        " ingest.relations =60,ingest.user=0,query=-120,unknown=1,"
        "ingest.structure=nope,missing-equals,ingest.references=1.5"
    )
    for _ in range(2):
        corpus = RequestPriority(priority_settings)
        assert corpus.resolve("ingest.relations") == 60
        assert corpus.resolve("ingest.structure") == 100
        assert corpus.resolve("ingest.references") == 100
        assert corpus.resolve("query") == -120
        assert (
            RequestPriority(priority_settings, user_document=True).resolve(
                "ingest.vision"
            )
            == 0
        )
    warning.assert_called_once()
    assert len(warning.call_args.kwargs["entries"]) == 4


def test_unknown_kind_fails_and_missing_kind_warns(priority_settings, monkeypatch):
    warning = Mock()
    monkeypatch.setattr("src.api_clients.request_priority.log.warning", warning)
    priority = RequestPriority(priority_settings)
    with pytest.raises(LlmError, match="Неизвестный"):
        priority.resolve("typo")
    assert priority.resolve(None) == priority.resolve(None) == 0
    warning.assert_called_once_with("llm_priority_missing_kind")


def test_settings_environment(monkeypatch):
    monkeypatch.setenv("DVD_LLM_REQUEST_PRIORITY_ENABLED", "false")
    monkeypatch.setenv("DVD_LLM_REQUEST_PRIORITIES", "ingest.structure=8")
    settings = Settings(_env_file=None)
    assert settings.llm_request_priority_enabled is False
    assert settings.llm_request_priorities == "ingest.structure=8"


@pytest.mark.parametrize(
    "enabled,value,expected", [(True, 37, 37), (False, 37, None), (True, 0, None)]
)
def test_text_request_body(priority_settings, enabled, value, expected):
    priority_settings.llm_request_priority_enabled = enabled
    priority_settings.llm_request_priorities = f"ingest.structure={value}"
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return answer()

    with text_client(priority_settings, handler) as client:
        client.chat("system", "user", {}, kind="ingest.structure")
    if expected is None:
        assert "priority" not in bodies[0]
    else:
        assert bodies[0]["priority"] == expected


@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize(
    "message", ["priority scheduling is not enabled", "priority is not supported"]
)
def test_400_disables_priority_for_client_lifetime(
    priority_settings, tmp_path, monkeypatch, vision, message
):
    bodies = []
    warning = Mock()
    monkeypatch.setattr("src.api_clients.request_priority.log.warning", warning)
    priority_settings.ocr_cache_dir = str(tmp_path)

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": message}})
        return answer("description" if vision else "{}")

    if vision:
        client = VisionClient(priority_settings, transport=httpx.MockTransport(handler))
    else:
        client = text_client(priority_settings, handler)
    try:
        for i in range(2):
            if vision:
                client.describe(b"png", str(i))
            else:
                client.chat("system", "user", {}, kind="ingest.structure")
    finally:
        client.close()
    assert len(bodies) == 3
    assert bodies[0].pop("priority") == 100
    assert bodies[0] == bodies[1]
    assert "priority" not in bodies[2]
    warning.assert_called_once()


def test_unrelated_400_does_not_disable_priority(priority_settings):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return (
            httpx.Response(400, text="invalid schema") if len(bodies) == 1 else answer()
        )

    with text_client(priority_settings, handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.chat("s", "u", {}, kind="ingest.structure")
        client.chat("s", "u", {}, kind="ingest.structure")
    assert [body["priority"] for body in bodies] == [100, 100]


@pytest.mark.parametrize(
    "separate,user,enabled,expected",
    [
        (False, False, True, 100),
        (False, True, True, 20),
        (True, True, True, None),
        (False, True, False, None),
    ],
)
def test_vision_endpoint_priority(
    priority_settings, tmp_path, separate, user, enabled, expected
):
    priority_settings.ocr_cache_dir = str(tmp_path)
    priority_settings.llm_request_priority_enabled = enabled
    if separate:
        priority_settings.vision_base_url = priority_settings.llm_base_url
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return answer("description")

    client = VisionClient(
        priority_settings, transport=httpx.MockTransport(handler), user_document=user
    )
    try:
        client.describe(b"png", "image")
    finally:
        client.close()
    if expected is None:
        assert "priority" not in bodies[0]
    else:
        assert bodies[0]["priority"] == expected


def test_ocr_never_carries_priority(priority_settings, monkeypatch):
    monkeypatch.setattr("src.api_clients.ocr_client.time.sleep", lambda _: None)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(503) if len(bodies) == 1 else answer("[]")

    client = DotsOcrClient(
        priority_settings.llm_base_url,
        model="dots.ocr",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.layout(b"png") == []
    finally:
        client.close()
    assert len(bodies) == 2
    assert all("priority" not in body for body in bodies)


def test_user_ingestion_pool_workers_are_isolated_from_corpus(
    priority_settings,
    fake_redis,
    fake_qdrant,
    fake_document_storage,
    monkeypatch,
    tmp_path,
):
    deps = SimpleNamespace(
        settings=priority_settings,
        qdrant=fake_qdrant,
        redis=RedisClient(priority_settings),
        user_document_storage=fake_document_storage,
        jobs=Mock(),
        parser=Mock(),
        structure=Mock(),
        hierarchy=Mock(),
        version_detector=Mock(),
        reference_extractor=Mock(),
        reference_resolver=Mock(),
        publisher=SimpleNamespace(enabled=False),
    )
    user = build_user_ingestion_from_deps(deps, user_id="u1", project_id="p1")
    corpus = IngestionService(
        deps.parser,
        deps.structure,
        deps.hierarchy,
        deps.version_detector,
        deps.reference_extractor,
        deps.reference_resolver,
        deps.qdrant,
        Mock(),
        fake_document_storage,
        deps.jobs,
        priority_settings,
    )
    barrier = Barrier(4)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(
            (body["messages"][0]["content"], body["priority"], current_thread().name)
        )
        barrier.wait(timeout=10)
        return answer('{"nodes": [{"id": 0}]}')

    monkeypatch.setattr(
        "src.dvd_service.services.dvd_service.create_llm",
        lambda: text_client(priority_settings, handler),
    )

    def run_job(ingestion, label):
        with ingestion._llm() as client:
            return list(
                map_concurrent(
                    lambda _: chat_window(
                        client,
                        label,
                        ["text"],
                        STRUCT_SCHEMA,
                        "nodes",
                        kind="ingest.structure",
                    ),
                    range(2),
                    max_workers=2,
                )
            )

    with ThreadPoolExecutor(2) as pool:
        jobs = [
            pool.submit(run_job, user, "user"),
            pool.submit(run_job, corpus, "corpus"),
        ]
        assert all(len(job.result()) == 2 for job in jobs)
    assert len(bodies) == 4
    assert all(
        priority == (20 if label.startswith("user") else 100)
        for label, priority, _ in bodies
    )
    assert all(thread.startswith("dvd-llm") for _, _, thread in bodies)
    # The same origin must also reach figure workers and amendment clients.
    priority_settings.ocr_cache_dir = str(tmp_path)
    priority_settings.vision_enabled = True
    priority_settings.vision_concurrency = 2
    figure_bodies = []

    def vision_handler(request):
        figure_bodies.append((json.loads(request.content), current_thread().name))
        return answer("description")

    monkeypatch.setattr(
        "src.dvd_service.services.dvd_service.VisionClient",
        lambda settings, **kwargs: VisionClient(
            settings, transport=httpx.MockTransport(vision_handler), **kwargs
        ),
    )
    for ingestion, expected in ((user, 20), (corpus, 100)):
        # User ingestion owns a settings copy made before the changes above.
        ingestion.settings.ocr_cache_dir = str(tmp_path)
        ingestion.settings.vision_enabled = True
        ingestion.settings.vision_concurrency = 2
        raw = [
            {
                "category": "Image",
                "text": "figure",
                "image_bytes": b"png",
                "figure": {"sha256": f"{expected}-{i}", "caption": ""},
            }
            for i in range(2)
        ]
        ingestion._describe_figures(raw, Mock())
        assert [body["priority"] for body, _ in figure_bodies[-2:]] == [expected] * 2
        assert all(thread != current_thread().name for _, thread in figure_bodies[-2:])
        service = AmendmentService(ingestion, Mock(), ingestion.settings)
        with service._llm() as client:
            assert client.priority.resolve("ingest.amendments") == expected
