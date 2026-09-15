"""Replay real parsed SP309 hits through gMART context, answer and critic locally.

No vector ranking evaluation: the two retrieved hits are fixed from local DOCX.
Only --live sends their text to the user-approved LLM endpoint.
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/preserve-tree-2026-09-15"
GMART = ROOT.parent / "gMART-worktrees/fix-document-resolution"
sys.path.insert(0, str(GMART))

import tests.conftest  # local service defaults, no external storage
from src.agents.services.dvd.dvd_context import (
    SOURCE_SEPARATOR,
    TREE_PREFIX,
    DvdContextBuilder,
    source_records,
)


def fixture():
    data = json.loads(
        (OUT / "corpus/СП_309.1325800.2017_с_И1.docx.nodes.json").read_text(
            encoding="utf8"
        )
    )["final"]
    by_id = {n["id"]: n for n in data}
    hits = []
    for node in data:
        if node["numbering"] not in {"8.1.1", "8.1.2"}:
            continue
        path, parent = [], node
        while parent is not None:
            if parent["type"] != "document":
                part = " ".join(
                    filter(None, [parent["numbering"], parent.get("fragment_name")])
                )
                if part:
                    path.append(part)
            parent = by_id.get(parent["parent_id"])
        hits.append(
            dict(
                node,
                doc_id="local-sp309",
                name="СП 309.1325800.2017 Здания театрально-зрелищные. Правила проектирования",
                version="2017 с Изменением № 1",
                structure_path=path[::-1],
            )
        )
    assert len(hits) == 2
    return hits


async def live(context):
    from src.agents.model_clients.openai_adapter import OpenAiCompatAdapter
    from src.agents.services.dvd.context_reducer import DvdContextReducer
    from src.agents.services.dvd.dvd_rag_service import DvdRagService
    from src.agents.services.dvd.dvd_reasoning import AnswerCritic
    from tests.helpers import answer_text

    adapter = OpenAiCompatAdapter(
        "http://10.32.11.27:8001/v1", timeout=180, think_effort="low"
    )
    service = DvdRagService.__new__(DvdRagService)
    service.llm_client = adapter
    service.context_reducer = DvdContextReducer(adapter)
    query = "Какие общие требования предъявляются к санитарно-гигиеническим условиям и материалам театрально-зрелищных зданий? Укажи источник для каждого требования."
    try:
        attempts, revision_note = [], None
        async with service.context_reducer.model_window("gpt-oss-20b"):
            # Match the production limit: an unapproved draft is never final.
            for iteration in range(1, 4):
                events = [
                    event
                    async for event in service._generate_answer(
                        "gpt-oss-20b",
                        query,
                        context,
                        0,
                        [],
                        iteration,
                        revision_note=revision_note,
                    )
                ]
                answer = answer_text(events)
                verdict = await AnswerCritic(adapter).review(
                    "gpt-oss-20b", query, context, answer
                )
                attempts.append(dict(answer=answer, verdict=verdict.model_dump()))
                if verdict.satisfied:
                    break
                revision_note = verdict.critique
        # The production prompt also permits an explicit document + clause address.
        normalized = re.sub(r"\s+", " ", answer)
        result = dict(
            query=query,
            answer=answer,
            verdict=verdict.model_dump(),
            attempts=attempts,
            numbered_sources_cited=all(label in answer for label in ("[1]", "[2]")),
            both_sources_cited=all(
                label in answer
                or ("СП 309.1325800.2017" in normalized and number in answer)
                for label, number in (("[1]", "8.1.1"), ("[2]", "8.1.2"))
            ),
            unknown_source_labels=AnswerCritic._literal_defects(context, answer),
        )
        (OUT / "live-context.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        assert (
            verdict.satisfied
            and result["both_sources_cited"]
            and not result["unknown_source_labels"]
        )
    finally:
        await adapter.client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    builder, hits = DvdContextBuilder(), fixture()
    compact = builder.build_context(hits)
    flat = "".join(
        builder._format_hit(i, h).replace(SOURCE_SEPARATOR, "\\u001e")
        + SOURCE_SEPARATOR
        for i, h in enumerate(builder.ordered_hits(hits), 1)
    )
    before, after = source_records(flat), source_records(compact)
    assert compact.startswith(TREE_PREFIX)
    assert list(before) == list(after)
    assert all(before[label][1] == after[label][1] for label in before)
    for name, text in (
        ("context-before.txt", flat),
        ("context-after.txt", compact),
        ("user-full-quote.txt", builder.full_quote(hits)),
    ):
        (OUT / name).write_text(text, encoding="utf8")
    metrics = dict(
        source_count=len(hits),
        source_bodies_identical=True,
        utf8_bytes_before=len(flat.encode()),
        utf8_bytes_after=len(compact.encode()),
        reduction_percent=round(
            100 * (1 - len(compact.encode()) / len(flat.encode())), 2
        ),
    )
    (OUT / "context-metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf8"
    )
    print(json.dumps(metrics), flush=True)
    if args.live:
        asyncio.run(live(compact))


if __name__ == "__main__":
    main()
