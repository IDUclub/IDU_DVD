"""Freeze literal source-address references independently of production parsers.

The corpus is already extracted. Section/article boundaries below are reviewed
against source text; numeric addresses are transcribed literally, not inferred by LLM.
Keep this reference frozen when tuning implementation. See protocol.md for scope.
"""

import json
import random
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / "docs/diagnostics/structure-accuracy"
corpus = json.loads((out / "source-corpus.json").read_text(encoding="utf8"))
gold = []
for name, rows in corpus.items():
    if name.startswith("СП_55."):
        selected = []
        for i, b in enumerate(rows):
            m = re.match(r"^(\d+\.\d+(?:\.\d+)*)\s+(?!\()", b["text"])
            if m and b["category"] != "Table":
                selected.append(
                    {
                        "document": name,
                        "row": i,
                        "number": m[1],
                        "parent": m[1].rsplit(".", 1)[0],
                        "source": b["text"],
                        "split": "calibration",
                    }
                )
        gold.extend(selected)
    elif name.startswith("4b8213aa"):
        chapter = ""
        article = ""
        section = ""
        for i, b in enumerate(rows):
            t = b["text"]
            if t.startswith("РАЗДЕЛ "):
                section = t.split()[1]
                chapter = ""
                article = ""
            ch = re.match(r"^Глава (\d+)\.", t)
            if ch:
                chapter = ch[1]
                article = ""
            h = re.fullmatch(r"Статья (\d+(?:_\d+)?)", t)
            m = re.match(r"^(\d+(?:\.\d+)*)\.\s", t)
            if h:
                article = h[1]
                gold.append(
                    {
                        "document": name,
                        "row": i,
                        "number": article,
                        "parent": chapter or section,
                        "source": t,
                        "split": "calibration",
                    }
                )
            elif m:
                gold.append(
                    {
                        "document": name,
                        "row": i,
                        "number": m[1],
                        "parent": article or section,
                        "source": t,
                        "split": "calibration",
                    }
                )
    elif any(name.startswith(p) for p in ["СП_19.", "СП_257.", "СП_309.", "СП_397."]):
        candidates = []
        for i, b in enumerate(rows):
            m = re.match(r"^((?:[А-ЯA-Z]\.)?\d+(?:\.\d+)+)\s+(?!\()", b["text"])
            if m and b["category"] != "Table":
                candidates.append(
                    {
                        "document": name,
                        "row": i,
                        "number": m[1],
                        "parent": m[1].rsplit(".", 1)[0],
                        "source": b["text"],
                        "split": "validation",
                    }
                )
        gold.extend(random.Random(915).sample(candidates, min(50, len(candidates))))
(out / "gold.json").write_text(
    json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf8"
)
print("Frozen rows", len(gold))
print(
    "Calibration",
    sum(x["split"] == "calibration" for x in gold),
    "validation",
    sum(x["split"] == "validation" for x in gold),
)
