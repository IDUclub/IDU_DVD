"""Compare the latest dev parse against its downloaded source, without LLM calls."""

import collections
import json
import re
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "docs/diagnostics/dev-parsing-2026-09-14"
d = json.loads(
    (P / "a7e1c951-c663-4366-aec8-6c3ebffbef29.json").read_text(encoding="utf8")
)
blocks = json.loads((P / "latest-source-blocks.json").read_text(encoding="utf8"))
source = (P / "latest-source.txt").read_text(encoding="utf8")
fs = d["fragments"]
byid = {f["id"]: f for f in fs}
norm = lambda s: " ".join(s.split())
coverage = bytearray(len(source))
checks = []
for f in fs:
    a, b = f["char_start"], f["char_end"]
    if a is None or b is None:
        continue
    original = source[a:b]
    coverage[a:b] = b"\x01" * (b - a)
    clean = norm(original)
    text = norm(f["text"])
    stripped = (
        re.sub(r"^" + re.escape(f["numbering"]) + r"[.)]?\s*", "", clean)
        if f["numbering"]
        else clean
    )
    checks.append(
        {
            "order": f["order"],
            "numbering": f["numbering"],
            "exact_after_whitespace_and_own_number": text in [clean, stripped],
            "source": original,
            "text": f["text"],
        }
    )

clause_checks = []
pos = 0
for block in blocks:
    text = block["text"]
    m = re.match(r"^(\d+\.\d+(?:\.\d+)*)\s+", text)
    if m:
        num = m[1]
        matches = [f for f in fs if f["numbering"] == num]
        overlaps = [
            f
            for f in fs
            if f["char_start"] is not None
            and f["char_start"] < pos + len(text)
            and f["char_end"] > pos
        ]
        clause_checks.append(
            {
                "number": num,
                "source": text,
                "exact_number_nodes": len(matches),
                "covering_orders": [f["order"] for f in overlaps],
                "direct_orders": [f["order"] for f in matches],
                "text_present": norm(text[m.end() :])
                in norm(" ".join(f["text"] for f in overlaps)),
                "parents": [
                    {
                        "order": f["order"],
                        "type": f["type"],
                        "parent_number": byid.get(f["parent_id"], {}).get("numbering"),
                        "parent_type": byid.get(f["parent_id"], {}).get("type"),
                    }
                    for f in matches
                ],
            }
        )
    pos += len(text) + 1

gaps = []
for m in re.finditer(r"\x00+", bytes(coverage).decode("latin1")):
    t = source[m.start() : m.end()]
    if t.strip():
        gaps.append({"start": m.start(), "end": m.end(), "text": t})
summary = {
    "fragments": len(fs),
    "grounded": len(checks),
    "source_chars": len(source),
    "source_nonspace": sum(not c.isspace() for c in source),
    "uncovered_nonspace": sum(
        not c.isspace() and not coverage[i] for i, c in enumerate(source)
    ),
    "exact_span_text": sum(x["exact_after_whitespace_and_own_number"] for x in checks),
    "numbered_source_blocks": len(clause_checks),
    "unique_numbers": len(set(x["number"] for x in clause_checks)),
    "numbers_with_direct_node": sum(x["exact_number_nodes"] > 0 for x in clause_checks),
    "source_blocks_text_present": sum(x["text_present"] for x in clause_checks),
    "types": dict(collections.Counter(f["type"] for f in fs)),
}
result = {
    "summary": summary,
    "gaps": gaps,
    "clause_checks": clause_checks,
    "span_checks": checks,
}
(P / "comparison.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
)
print(json.dumps(summary, ensure_ascii=False, indent=2))
print(
    "MISSING NUMBERS",
    [
        (x["number"], x["covering_orders"])
        for x in clause_checks
        if not x["exact_number_nodes"]
    ],
)
print("GAPS", json.dumps(gaps, ensure_ascii=False)[:2500])
print(
    "TEXT MISMATCHES",
    [
        (x["order"], x["numbering"])
        for x in checks
        if not x["exact_after_whitespace_and_own_number"]
    ],
)
