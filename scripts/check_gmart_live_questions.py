"""Exercise the actual gMART pipeline with live LLM and local DVD search.

ChatStorage, Urban API and Redis are isolated; no dev writes. The DVD MCP
transport is replaced by a subprocess bridge to the real local search service.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

DVD = Path(__file__).resolve().parents[1]
GMART = DVD.parent / "gMART-worktrees/fix-document-resolution"
OUT = Path(
    os.getenv(
        "GMART_VERIFY_OUT",
        str(DVD / "docs/diagnostics/accuracy-confirmation-2026-09-15/gmart"),
    )
)
sys.path.insert(0, str(GMART))

import fakeredis.aioredis

import tests.conftest  # test-only service URL defaults; no live storage
from src.agents.model_clients.openai_adapter import OpenAiCompatAdapter
from src.agents.services.dvd.dvd_rag_service import DvdRagService
from src.agents.services.pipeline_state import PipelineStateStore
from tests.helpers import FakeUrbanApiClient, answer_text


class LocalDvd:
    def __init__(self):
        self.calls = []

    async def search_fragments(self, request, *, mode):
        entry = dict(mode=mode, request=request.copy())
        self.calls.append(entry)
        proc = await asyncio.create_subprocess_exec(
            str(DVD / ".venv/Scripts/python.exe"),
            "-X",
            "utf8",
            "-m",
            "scripts.confirmation_search_bridge",
            cwd=str(DVD),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(
            json.dumps({"request": request}, ensure_ascii=False).encode()
        )
        if proc.returncode:
            raise RuntimeError(stderr.decode("utf8", "replace")[-2000:])
        result = json.loads(stdout.decode("utf8").splitlines()[-1])
        entry["response"] = result
        return result

    @staticmethod
    def tool_name_for_kind(kind):
        return "search_all"

    async def search(self, *args, **kwargs):
        self.calls.append(dict(mode="unexpected_semantic", args=args, kwargs=kwargs))
        raise RuntimeError(
            "Exact-clause verification reached semantic search; no vector fallback configured"
        )


CASES = [
    ("sp55_short", "Что написано в пункте 3.3 СП 55?"),
    (
        "sp55_full",
        "Что написано в пункте 3.3 СП 55.13330.2016 Дома жилые одноквартирные?",
    ),
    ("point_then_section", "Что написано в пункте 3.3 в разделе 3 СП 55?"),
    ("section_then_point", "В разделе 3 СП 55 что написано в пункте 3.3?"),
    ("nested_children", "Процитируй пункт 10.6 СП 55 вместе с подпунктами."),
    ("wrong_document", "Что написано в пункте 3.3 СП 550?"),
    ("missing_clause", "Что написано в пункте 999.9 СП 55?"),
    ("repeated", "Что написано в пункте 3.3 СП 999?"),
    ("repeated_explicit_path", "Что написано в пункте 3.3 раздела II СП 999?"),
    ("constitution_article", "Что написано в статье 19 конституции?"),
    ("constitution_part", "Что написано в части 1 статьи 19 Конституции РФ?"),
    (
        "constitution_part_reordered",
        "Конституция РФ: статья 19, часть 1 — что написано?",
    ),
]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    adapter = OpenAiCompatAdapter(
        "http://10.32.11.27:8001/v1", timeout=300, think_effort="low"
    )
    results = []
    previous = None
    try:
        cases = CASES + [("choice_followup", "второй вариант")]
        selected = set(filter(None, os.getenv("GMART_VERIFY_CASES", "").split(",")))
        if selected:
            cases = [(i, q) for i, q in cases if i in selected]
        repeated_path = OUT / "repeated.json"
        if repeated_path.exists():
            previous = json.loads(repeated_path.read_text(encoding="utf8"))
        for case_id, query in cases:
            dest = OUT / (case_id + ".json")
            if dest.exists():
                data = json.loads(dest.read_text(encoding="utf8"))
                results.append(data)
                if case_id == "repeated":
                    previous = data
                print("EXISTS", case_id, flush=True)
                continue
            redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            with patch(
                "src.agents.model_clients.base_client.build_llm_adapter",
                return_value=adapter,
            ):
                service = DvdRagService(
                    "http://unused",
                    Mock(),
                    FakeUrbanApiClient(),
                    PipelineStateStore(redis),
                )
            history = []
            if case_id == "choice_followup" and previous:
                history = [
                    {"role": "user", "content": previous["query"]},
                    {"role": "assistant", "content": previous["answer"]},
                ]
            service.create_chat = AsyncMock(
                return_value=("local-verification", "Проверка")
            )
            service.get_chat_messages = AsyncMock(
                return_value=SimpleNamespace(messages=history)
            )
            service.add_single_message = AsyncMock()
            service._schedule_persist_answer = Mock()
            client = LocalDvd()
            events = []
            start = time.monotonic()
            print("START", case_id, flush=True)
            try:

                async def collect():
                    async for event in service.run_document_qa_pipeline(
                        dvd_mcp_client=client,
                        token="local-test",
                        model="gpt-oss-20b",
                        temperature=0,
                        user_query=query,
                        chat_id="local-" + case_id,
                    ):
                        events.append(event)

                await asyncio.wait_for(collect(), timeout=420)
                error = None
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc)
            data = dict(
                case=case_id,
                query=query,
                answer=answer_text(events),
                calls=client.calls,
                events=events,
                error=error,
                elapsed_seconds=round(time.monotonic() - start, 2),
            )
            dest.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf8",
            )
            results.append(data)
            if case_id == "repeated":
                previous = data
            print(
                "COMPLETE",
                case_id,
                "calls",
                len(client.calls),
                "error",
                error,
                "answer",
                data["answer"][:160],
                flush=True,
            )
            await redis.aclose()
    finally:
        await adapter.client.close()
    (OUT / ("subset-results.json" if selected else "results.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf8"
    )


if __name__ == "__main__":
    asyncio.run(main())
