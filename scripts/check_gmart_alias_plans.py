"""Live planner checks for abbreviated legal document names and typed addresses."""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "gMART-worktrees/fix-document-resolution"))
import tests.conftest
from src.agents.model_clients.openai_adapter import OpenAiCompatAdapter
from src.agents.services.dvd.dvd_reasoning import RetrievalPlanner

CASES = [
    (
        "grad_code",
        "Что написано в пункте 3.3 град кодекса?",
        "Градостроительный кодекс Российской Федерации",
    ),
    (
        "grk_rf",
        "Что написано в пункте 3.3 ГрК РФ?",
        "Градостроительный кодекс Российской Федерации",
    ),
    (
        "constitution",
        "Что написано в статье 19 конституции?",
        "Конституция Российской Федерации",
    ),
    (
        "constitution_rf",
        "Что написано в статье 19 Конституции РФ?",
        "Конституция Российской Федерации",
    ),
    (
        "constitution_part",
        "Что написано в части 1 статьи 19 Конституции Российской Федерации?",
        "Конституция Российской Федерации",
    ),
    ("short_point", "Что написано в п. 3.3 СП 55?", "СП 55"),
]


async def main():
    out = Path(
        os.getenv(
            "GMART_VERIFY_OUT",
            str(ROOT / "docs/diagnostics/accuracy-confirmation-2026-09-15/gmart"),
        )
    )
    out.mkdir(parents=True, exist_ok=True)
    adapter = OpenAiCompatAdapter(
        "http://10.32.11.27:8001/v1", timeout=180, think_effort="low"
    )
    planner = RetrievalPlanner(adapter)
    results = []
    try:
        for case, query, document in CASES:
            plan = await planner.build_plan("gpt-oss-20b", query, history=[])
            data = dict(
                case=case,
                query=query,
                expected_document=document,
                plan=plan.model_dump(mode="json"),
            )
            results.append(data)
            print(json.dumps(data, ensure_ascii=False), flush=True)
            (out / "alias-plans.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8"
            )
    finally:
        await adapter.client.close()


if __name__ == "__main__":
    asyncio.run(main())
